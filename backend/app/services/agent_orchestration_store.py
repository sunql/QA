"""编排运行的原子 claim、租约与执行守卫（Task 8）。

职责：
- ``claimNext``：先把**一条**过期 RUNNING（旧 owner 已死）恢复为 QUEUED，
  再从 QUEUED 中按 id 顺序 ``FOR UPDATE SKIP LOCKED`` 选一条并条件 UPDATE
  为 RUNNING。两个短事务、各自提交，均不跨越任何 LLM/工具调用；
- ``renewLease``：owner + RUNNING + 租约仍有效 的条件续租，过期/他人一律失败；
- ``assertExecutionAllowed``：执行前在行锁下重读 run，依次校验
  ``cancel_requested`` → owner → 租约未过期 → RUNNING → iteration/tool/cost/time
  预算。租约/取消抛 ``LeaseLostError``（控制流，不是暂停），预算耗尽抛
  ``OrchestrationPauseError(BUDGET_EXHAUSTED, detail={"budget": ...})``。

锁顺序（跨任务不变量）：本模块只锁 run 行；若同事务后续还要锁 step 行，
必须先拿 run 锁（Task 6 计量、Task 9 step 写入均遵守）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
)
from app.services.agent_orchestration_types import (
    AskUserAction,
    CallToolAction,
    FinishAction,
    LeaseLostError,
    OrchestrationPauseError,
    PauseCode,
    PlannerAction,
    RunStatus,
    StepKind,
    StepStatus,
)
from app.utils.json_safe import jsonSafe

logger = logging.getLogger(__name__)

MSG_BUDGET_EXHAUSTED = "编排运行预算已耗尽"

# LeaseLostError.reason 的稳定取值（Task 9 Runner 与测试依赖）。
LOST_RUN_MISSING = "run_missing"
LOST_CANCELLED = "cancelled"
LOST_OWNER_MISMATCH = "lease_owner_mismatch"
LOST_EXPIRED = "lease_expired"
LOST_NOT_RUNNING = "not_running"


def _raiseBudgetPause(budgetKey: str) -> None:
    """预算耗尽：canonical detail 键固定为 iteration/tool/cost/time。"""
    raise OrchestrationPauseError(
        PauseCode.BUDGET_EXHAUSTED.value,
        MSG_BUDGET_EXHAUSTED,
        detail={"budget": budgetKey},
    )


class AgentOrchestrationStore:
    """QUEUED run 的原子 claim、租约续期与执行前守卫。

    本类同时实现 ``ExecutionGuard`` Protocol（Task 4 的工具执行器注入使用）。
    """

    async def claimNext(
        self,
        session: AsyncSession,
        workerId: str,
        leaseSeconds: int,
        now: datetime,
    ) -> AgentOrchestrationRun | None:
        """恢复一条过期 run 后原子 claim 一条 QUEUED；无候选返回 None。

        claim 在返回前已提交：调用方拿到 run 时租约已经对外可见，且本地
        事务已结束（Worker 随后才调用 Runner，期间不持有数据库事务）。
        """
        await self._recoverOneExpired(session, now)

        runId = (
            await session.execute(
                select(AgentOrchestrationRun.id)
                .where(AgentOrchestrationRun.status == RunStatus.QUEUED.value)
                .order_by(AgentOrchestrationRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if runId is None:
            await session.commit()
            return None

        claimed = (
            await session.execute(
                update(AgentOrchestrationRun)
                .where(
                    AgentOrchestrationRun.id == runId,
                    AgentOrchestrationRun.status == RunStatus.QUEUED.value,
                )
                .values(
                    status=RunStatus.RUNNING.value,
                    lease_owner=workerId,
                    lease_until=now + timedelta(seconds=leaseSeconds),
                    # 每次 QUEUED -> RUNNING 都重开计时窗口：paused/queued 时间与
                    # 死掉的旧 owner 消耗的窗口都不计新 owner 头上。
                    started_at=now,
                    revision=AgentOrchestrationRun.revision + 1,
                )
                .returning(AgentOrchestrationRun)
            )
        ).scalar_one_or_none()
        if claimed is None:
            # 候选行在锁内已不是 QUEUED（理论上不会发生）：放弃本轮，不误报 claim。
            await session.commit()
            return None

        if claimed in session:
            # commit 后仍可安全读取字段，不依赖调用方的 expire_on_commit 配置。
            session.expunge(claimed)
        await session.commit()
        return claimed

    async def renewLease(
        self,
        session: AsyncSession,
        runId: int,
        workerId: str,
        leaseSeconds: int,
        now: datetime,
    ) -> bool:
        """按 owner + RUNNING + 租约仍有效 续租；他人或已过期一律返回 False。"""
        renewed = (
            await session.execute(
                update(AgentOrchestrationRun)
                .where(
                    AgentOrchestrationRun.id == runId,
                    AgentOrchestrationRun.lease_owner == workerId,
                    AgentOrchestrationRun.status == RunStatus.RUNNING.value,
                    AgentOrchestrationRun.lease_until.is_not(None),
                    AgentOrchestrationRun.lease_until > now,
                )
                .values(lease_until=now + timedelta(seconds=leaseSeconds))
                .returning(AgentOrchestrationRun.id)
            )
        ).scalar_one_or_none()
        await session.commit()
        return renewed is not None

    async def assertExecutionAllowed(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
        leaseOwner: str,
        now: datetime,
        *,
        skipIterationBudget: bool = False,
    ) -> None:
        """执行前守卫：行锁下重读，先取消/租约，后四类预算。

        ``populate_existing`` 不可省：调用方通常早已把这条 run 读进 identity
        map，SQLAlchemy 默认不刷新既有实例属性 ⇒ 拿到锁却按陈旧值放行。

        ``skipIterationBudget`` 只给 Task 9 的 Runner 内部适配器使用：Runner 在
        每轮入口已按 iteration 预算放行并**预占**了本轮（建 PLAN 时 +1），工具
        prepare 期间的 iteration 判定必然是本轮自身造成的，否则最后一轮永远
        无法执行工具。其余三类预算（tool/cost/time）照常检查。
        """
        current = (
            await session.execute(
                select(AgentOrchestrationRun)
                .where(AgentOrchestrationRun.id == run.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise LeaseLostError(LOST_RUN_MISSING)
        if current.cancel_requested:
            raise LeaseLostError(LOST_CANCELLED)
        if current.lease_owner != leaseOwner:
            raise LeaseLostError(LOST_OWNER_MISMATCH)
        if current.lease_until is None or current.lease_until <= now:
            raise LeaseLostError(LOST_EXPIRED)
        if current.status != RunStatus.RUNNING.value:
            raise LeaseLostError(LOST_NOT_RUNNING)
        self._assertBudgets(current, now, skipIteration=skipIterationBudget)

    @staticmethod
    def _assertBudgets(
        run: AgentOrchestrationRun,
        now: datetime,
        *,
        skipIteration: bool = False,
    ) -> None:
        """iteration → tool → cost → time（detail 只写 canonical 四键）。"""
        if not skipIteration and run.iteration_count >= run.max_iterations:
            _raiseBudgetPause("iteration")
        if run.tool_call_count >= run.max_tool_calls:
            _raiseBudgetPause("tool")
        # Numeric(16,8) 经 PG 回来是 Decimal；绝不与 float 比较。
        if Decimal(run.cost) >= Decimal(run.max_cost_usd):
            _raiseBudgetPause("cost")
        if (
            run.started_at is not None
            and now - run.started_at >= timedelta(seconds=run.timeout_seconds)
        ):
            _raiseBudgetPause("time")

    async def _recoverOneExpired(self, session: AsyncSession, now: datetime) -> None:
        """把最老的一条过期 RUNNING 恢复为 QUEUED（独立短事务并提交）。

        UPDATE 再次断言 ``status=RUNNING AND lease_until <= now``：即使有并发的
        恢复者/claim 者插进来，也不会清掉新 owner 刚写下的租约。
        """
        expiredId = (
            await session.execute(
                select(AgentOrchestrationRun.id)
                .where(
                    AgentOrchestrationRun.status == RunStatus.RUNNING.value,
                    AgentOrchestrationRun.lease_until.is_not(None),
                    AgentOrchestrationRun.lease_until <= now,
                )
                .order_by(AgentOrchestrationRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if expiredId is not None:
            recovered = (
                await session.execute(
                    update(AgentOrchestrationRun)
                    .where(
                        AgentOrchestrationRun.id == expiredId,
                        AgentOrchestrationRun.status == RunStatus.RUNNING.value,
                        AgentOrchestrationRun.lease_until.is_not(None),
                        AgentOrchestrationRun.lease_until <= now,
                    )
                    .values(
                        status=RunStatus.QUEUED.value,
                        lease_owner=None,
                        lease_until=None,
                        started_at=None,
                        revision=AgentOrchestrationRun.revision + 1,
                    )
                    .returning(AgentOrchestrationRun.id)
                )
            ).scalar_one_or_none()
            if recovered is not None:
                logger.info("恢复过期 RUNNING run %s -> QUEUED（旧租约已过期）", recovered)
        await session.commit()

    # ------------------------------------------------------------------
    # Task 9: step allocation
    # ------------------------------------------------------------------

    async def getOrCreatePlanStep(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
        leaseOwner: str,
        now: datetime,
    ) -> AgentOrchestrationStep:
        """返回本轮的 PLAN 步骤：已有 RUNNING PLAN 直接复用，否则新建并计数。

        复用的语义（崩溃重放）是「同一个 PLAN 只有一次 iteration 计数」：
        旧 worker 建好 RUNNING PLAN 后崩溃，新 worker 重规划时不得再次 +1。
        新建时 `iteration_no` 必须是 `iteration_count + 1`，这样 PLAN 与它的
        TOOL 共享同一个迭代号，`getOrCreateToolStep` 才能按键找到唯一 TOOL。
        """
        current = await self._lockLeasedRun(
            session, run.id, leaseOwner, now, rejectCancel=True
        )
        existing = (
            await session.execute(
                select(AgentOrchestrationStep)
                .where(
                    AgentOrchestrationStep.run_id == current.id,
                    AgentOrchestrationStep.kind == StepKind.PLAN.value,
                    AgentOrchestrationStep.status == StepStatus.RUNNING.value,
                )
                .order_by(AgentOrchestrationStep.step_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            session.expunge(existing)
            await session.commit()
            return existing

        step = AgentOrchestrationStep(
            run_id=current.id,
            step_no=await self._nextStepNo(session, current.id),
            iteration_no=int(current.iteration_count or 0) + 1,
            kind=StepKind.PLAN.value,
            status=StepStatus.RUNNING.value,
            started_at=now,
        )
        session.add(step)
        current.iteration_count = int(current.iteration_count or 0) + 1
        current.revision = int(current.revision or 1) + 1
        await session.flush()
        session.expunge(step)
        await session.commit()
        return step

    async def getOrCreateToolStep(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        leaseOwner: str,
        now: datetime,
        toolName: str,
        args: dict,
    ) -> AgentOrchestrationStep:
        """返回本轮唯一的 TOOL 步骤；首次创建时才增加 `tool_call_count`。

        唯一键是 `(run_id, iteration_no, kind=TOOL)`：崩溃重放与「PLAN 已提交
        但 TOOL 尚未创建」的重建都落到同一行，因此一次工具动作永远只有一条
        TOOL 记录、只计一次预算。重放时参数必须与首次落库完全一致，不一致
        属于编程错误（状态机不变量），直接失败而不是猜测用户意图。
        """
        current = await self._lockLeasedRun(
            session, run.id, leaseOwner, now, rejectCancel=True
        )
        expectedArgs = jsonSafe(dict(args))
        existing = (
            await session.execute(
                select(AgentOrchestrationStep).where(
                    AgentOrchestrationStep.run_id == current.id,
                    AgentOrchestrationStep.iteration_no == planStep.iteration_no,
                    AgentOrchestrationStep.kind == StepKind.TOOL.value,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.tool_name != toolName or (existing.input_json or {}) != expectedArgs:
                raise RuntimeError("orchestration TOOL replay mismatch")
            session.expunge(existing)
            await session.commit()
            return existing

        step = AgentOrchestrationStep(
            run_id=current.id,
            step_no=await self._nextStepNo(session, current.id),
            iteration_no=planStep.iteration_no,
            kind=StepKind.TOOL.value,
            status=StepStatus.RUNNING.value,
            tool_name=toolName,
            input_json=expectedArgs,
            started_at=now,
        )
        session.add(step)
        current.tool_call_count = int(current.tool_call_count or 0) + 1
        current.revision = int(current.revision or 1) + 1
        await session.flush()
        session.expunge(step)
        await session.commit()
        return step

    async def completePlanStep(
        self,
        session: AsyncSession,
        runId: int,
        stepId: int,
        leaseOwner: str,
        now: datetime,
        action: PlannerAction,
    ) -> None:
        """把 RUNNING PLAN 提交为 SUCCEEDED 并原子落库动作字段。

        这是崩溃恢复的关键写入：CALL_TOOL 的 toolName/args、FINISH 的 answer、
        ASK_USER 的 question 都在这里一次提交。恢复方据此**不再次调用 LLM**
        就能重建动作。取消请求不阻止本次提交（取消只在下一个安全点生效）。
        """
        await self._lockLeasedRun(session, runId, leaseOwner, now, rejectCancel=False)
        step = await self._lockStep(session, runId, stepId)
        if step.kind != StepKind.PLAN.value or step.status != StepStatus.RUNNING.value:
            raise RuntimeError("orchestration PLAN step is not RUNNING")
        self._applyPlanAction(step, action)
        step.status = StepStatus.SUCCEEDED.value
        step.finished_at = now
        await session.commit()

    async def completeToolStep(
        self,
        session: AsyncSession,
        runId: int,
        stepId: int,
        leaseOwner: str,
        now: datetime,
        *,
        status: str,
        output: dict | None = None,
        errorCode: str | None = None,
        errorMessage: str | None = None,
    ) -> None:
        """把 RUNNING TOOL 提交为 SUCCEEDED/FAILED，并落库有界观测/清洗错误。

        取消请求不阻止本次提交：正在执行的只读工具已经跑完，先落库观测，
        下一次循环入口的安全点再统一转 CANCELLED（不丢已发生的只读工作）。
        """
        await self._lockLeasedRun(session, runId, leaseOwner, now, rejectCancel=False)
        step = await self._lockStep(session, runId, stepId)
        if step.kind != StepKind.TOOL.value or step.status != StepStatus.RUNNING.value:
            raise RuntimeError("orchestration TOOL step is not RUNNING")
        if status not in (StepStatus.SUCCEEDED.value, StepStatus.FAILED.value):
            raise RuntimeError("orchestration TOOL step status is not terminal")
        step.status = status
        step.output_json = jsonSafe(output) if output is not None else None
        step.error_code = errorCode
        step.error_message = errorMessage
        step.finished_at = now
        await session.commit()

    # ------------------------------------------------------------------
    # Task 9: run transitions
    # ------------------------------------------------------------------

    async def pauseRun(
        self,
        session: AsyncSession,
        runId: int,
        leaseOwner: str,
        code: str,
        detail: dict | None,
        now: datetime,
        *,
        consumedStepId: int | None = None,
    ) -> None:
        """RUNNING -> PAUSED：清租约但**不**写 finished_at（不是终态）。

        取消优先：若 `cancel_requested` 已置位，暂停不再成立，抛
        ``LeaseLostError(cancelled)`` 让 Runner 走取消收尾。`consumedStepId`
        用于把已产出 ASK_USER 的 PLAN 标为 SKIPPED —— 用户恢复后应重新规划，
        而不是把同一个问题再问一遍。
        """
        current = await self._lockLeasedRun(session, runId, leaseOwner, now, rejectCancel=False)
        if current.cancel_requested:
            raise LeaseLostError(LOST_CANCELLED)
        current.status = RunStatus.PAUSED.value
        current.pause_code = code
        current.pause_detail = jsonSafe(detail) if detail is not None else None
        current.lease_owner = None
        current.lease_until = None
        current.revision = int(current.revision or 1) + 1
        if consumedStepId is not None:
            consumed = await session.execute(
                update(AgentOrchestrationStep)
                .where(
                    AgentOrchestrationStep.id == consumedStepId,
                    AgentOrchestrationStep.run_id == runId,
                    AgentOrchestrationStep.kind == StepKind.PLAN.value,
                    AgentOrchestrationStep.status == StepStatus.SUCCEEDED.value,
                )
                .values(status=StepStatus.SKIPPED.value)
            )
            if consumed.rowcount != 1:
                raise RuntimeError("orchestration consumed PLAN step not found")
        await session.commit()

    async def succeedRun(
        self,
        session: AsyncSession,
        runId: int,
        leaseOwner: str,
        answer: str,
        now: datetime,
    ) -> None:
        """RUNNING -> SUCCEEDED（终态）：写答案、清租约、写 finished_at。"""
        current = await self._lockLeasedRun(session, runId, leaseOwner, now, rejectCancel=False)
        if current.cancel_requested:
            raise LeaseLostError(LOST_CANCELLED)
        current.status = RunStatus.SUCCEEDED.value
        current.final_answer = answer
        current.lease_owner = None
        current.lease_until = None
        current.finished_at = now
        current.revision = int(current.revision or 1) + 1
        await session.commit()

    async def cancelRun(
        self,
        session: AsyncSession,
        runId: int,
        leaseOwner: str,
        now: datetime,
    ) -> None:
        """RUNNING -> CANCELLED（终态）。条件 UPDATE：租约已易主则不动别人。

        返回前已提交；未命中（已终态或 owner 变了）只记 info —— 调用方本来
        就在停止推进的路径上，重复取消是幂等的。
        """
        cancelled = (
            await session.execute(
                update(AgentOrchestrationRun)
                .where(
                    AgentOrchestrationRun.id == runId,
                    AgentOrchestrationRun.status == RunStatus.RUNNING.value,
                    AgentOrchestrationRun.lease_owner == leaseOwner,
                )
                .values(
                    status=RunStatus.CANCELLED.value,
                    lease_owner=None,
                    lease_until=None,
                    finished_at=now,
                    revision=AgentOrchestrationRun.revision + 1,
                )
                .returning(AgentOrchestrationRun.id)
            )
        ).scalar_one_or_none()
        await session.commit()
        if cancelled is None:
            logger.info("run %s 取消未命中（已终态或租约已易主），忽略", runId)

    async def failRun(
        self,
        session: AsyncSession,
        runId: int,
        leaseOwner: str,
        errorCode: str,
        errorMessage: str,
        now: datetime,
    ) -> None:
        """RUNNING -> FAILED（终态）：仅用于「继续执行已不安全」的不变量错误。"""
        current = await self._lockLeasedRun(session, runId, leaseOwner, now, rejectCancel=False)
        current.status = RunStatus.FAILED.value
        current.error_code = errorCode
        current.error_message = errorMessage
        current.lease_owner = None
        current.lease_until = None
        current.finished_at = now
        current.revision = int(current.revision or 1) + 1
        await session.commit()

    # ------------------------------------------------------------------
    # Task 9: helpers
    # ------------------------------------------------------------------

    async def _lockLeasedRun(
        self,
        session: AsyncSession,
        runId: int,
        leaseOwner: str,
        now: datetime,
        *,
        rejectCancel: bool,
    ) -> AgentOrchestrationRun:
        """行锁重读 run 并校验 owner/租约/RUNNING（顺序与执行守卫一致）。"""
        current = (
            await session.execute(
                select(AgentOrchestrationRun)
                .where(AgentOrchestrationRun.id == runId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise LeaseLostError(LOST_RUN_MISSING)
        if rejectCancel and current.cancel_requested:
            raise LeaseLostError(LOST_CANCELLED)
        if current.lease_owner != leaseOwner:
            raise LeaseLostError(LOST_OWNER_MISMATCH)
        if current.lease_until is None or current.lease_until <= now:
            raise LeaseLostError(LOST_EXPIRED)
        if current.status != RunStatus.RUNNING.value:
            raise LeaseLostError(LOST_NOT_RUNNING)
        return current

    async def _lockStep(
        self,
        session: AsyncSession,
        runId: int,
        stepId: int,
    ) -> AgentOrchestrationStep:
        """按 run → step 的固定锁顺序锁住步骤行（调用前必须已锁 run）。"""
        step = (
            await session.execute(
                select(AgentOrchestrationStep)
                .where(
                    AgentOrchestrationStep.id == stepId,
                    AgentOrchestrationStep.run_id == runId,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if step is None:
            raise RuntimeError("orchestration step not found")
        return step

    @staticmethod
    async def _nextStepNo(session: AsyncSession, runId: int) -> int:
        maxNo = (
            await session.execute(
                select(func.max(AgentOrchestrationStep.step_no)).where(
                    AgentOrchestrationStep.run_id == runId
                )
            )
        ).scalar()
        return 0 if maxNo is None else int(maxNo) + 1

    @staticmethod
    def _applyPlanAction(step: AgentOrchestrationStep, action: PlannerAction) -> None:
        """把动作写进 PLAN 行；只写协议字段，不写任何隐藏推理。"""
        if isinstance(action, CallToolAction):
            step.action = "CALL_TOOL"
            step.tool_name = action.toolName
            step.input_json = jsonSafe(dict(action.args))
            step.plan_summary = action.summary
            return
        if isinstance(action, AskUserAction):
            step.action = "ASK_USER"
            step.answer = action.question
            step.plan_summary = action.reason
            return
        if isinstance(action, FinishAction):
            step.action = "FINISH"
            step.answer = action.answer
            return
        raise RuntimeError("orchestration planner action is not supported")
