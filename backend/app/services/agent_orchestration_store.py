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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_orchestration_models import AgentOrchestrationRun
from app.services.agent_orchestration_types import (
    LeaseLostError,
    OrchestrationPauseError,
    PauseCode,
    RunStatus,
)

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
    ) -> None:
        """执行前守卫：行锁下重读，先取消/租约，后四类预算。

        ``populate_existing`` 不可省：调用方通常早已把这条 run 读进 identity
        map，SQLAlchemy 默认不刷新既有实例属性 ⇒ 拿到锁却按陈旧值放行。
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
        self._assertBudgets(current, now)

    @staticmethod
    def _assertBudgets(run: AgentOrchestrationRun, now: datetime) -> None:
        """iteration → tool → cost → time（detail 只写 canonical 四键）。"""
        if run.iteration_count >= run.max_iterations:
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
