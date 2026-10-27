"""有界编排 Runner（Task 9）：把 claim 到的 run 推进到暂停/终态。

单轮流程（每轮都重新读库，绝不相信内存里的旧状态）：

1. **安全点**：执行守卫（取消 / owner / 租约 / 预算）+ 续租；
2. **恢复分派**：唯一事实来自已提交的步骤。RUNNING PLAN 原行重规划；
   RUNNING TOOL 用原行原参数重放；已提交 CALL_TOOL 的 PLAN 直接重建动作
   （不再调用 Planner）；已提交 FINISH / ASK_USER 直接收敛；
3. **规划轮**：建/复用 PLAN -> 计量客户端 -> Planner（有限纠正）-> prepare
   （协议错误在同一 PLAN 上纠正，权限拒绝立即暂停）-> 提交 PLAN -> 执行 TOOL。

不可持久化的东西一律不落库：只写协议字段（摘要/参数/答案）和**清洗后的**
错误码/错误文案；异常堆栈、连接串、provider 头只进服务端日志。

崩溃语义：状态机的每个持久化点在崩溃后都必须可重入。唯一键
`(run_id, step_no)` 与 `(run_id, iteration_no, kind=TOOL)` 保证一次动作只有
一行、只计一次预算；TOOL 输出提交后才进入下一轮规划。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import getSettings
from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
)
from app.infrastructure.database import getSessionFactory
from app.services.agent_orchestration_metering import MeteredLlmFactory
from app.services.agent_orchestration_planner import (
    ASK_USER,
    CALL_TOOL,
    FINISH,
    AgentOrchestrationPlanner,
)
from app.services.agent_orchestration_runner_support import (
    INFRASTRUCTURE_EXCEPTIONS,
    INFRASTRUCTURE_RETRY_LIMIT,
    MSG_PLANNER_INVALID,
    AgentOrchestrationRunReader,
    MeteredStep,
    RetryBudget,
    RunFinished,
    ToolCallGuard,
    isInfrastructureError,
)
from app.services.agent_orchestration_store import (
    LOST_CANCELLED,
    LOST_RUN_MISSING,
    AgentOrchestrationStore,
)
from app.services.agent_orchestration_tool_executor import (
    AgentOrchestrationToolExecutor,
)
from app.services.agent_orchestration_types import (
    AskUserAction,
    CallToolAction,
    FinishAction,
    LeaseLostError,
    OrchestrationPauseError,
    PauseCode,
    PlannerAction,
    PlannerProtocolError,
    StepKind,
    StepStatus,
)
from app.services.agent_orchestration_validation import serializeObservation
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.services.agent_tool_types import AgentToolContext, ToolResult

logger = logging.getLogger(__name__)

ERROR_INTERNAL = "INTERNAL_ERROR"
ERROR_TOOL_EXECUTION = "TOOL_EXECUTION_FAILED"

MSG_INTERNAL_ERROR = "编排运行遇到内部错误，已安全停止"
MSG_INFRASTRUCTURE_ERROR = "编排运行依赖的外部服务暂时不可用"
MSG_MODEL_UNAVAILABLE = "编排运行所需的模型配置当前不可用"
MSG_TOOL_FAILED = "工具执行失败"


class AgentOrchestrationRunner:
    """把 claim 到的 run 驱动到暂停或终态；零参构造供 Worker ``main`` 使用。"""

    def __init__(
        self,
        *,
        sessionFactory: async_sessionmaker[AsyncSession] | None = None,
        store: AgentOrchestrationStore | None = None,
        planner: Any | None = None,
        executor: Any | None = None,
        reader: AgentOrchestrationRunReader | None = None,
        settings: Any | None = None,
        meteredFactory: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._sessionFactory = sessionFactory or getSessionFactory()
        self._store = store or AgentOrchestrationStore()
        self._planner = planner or AgentOrchestrationPlanner()
        self._settings = settings or getSettings()
        self._reader = reader or AgentOrchestrationRunReader(self._sessionFactory)
        self._executor = executor or AgentOrchestrationToolExecutor(
            executionGuard=ToolCallGuard(self._store),
            toolRegistry=agent_tool_config_registry,
            runtime=AgentRuntimeService(),
        )
        self._meteredFactory = meteredFactory or MeteredLlmFactory
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def runClaimed(self, runId: int, workerId: str) -> None:
        """执行一个已 claim 的 run；租约丢失是控制流，不是错误。"""
        logger.info("编排 Runner 开始处理 run %s（worker=%s）", runId, workerId)
        try:
            await self._drive(runId, workerId)
        except RunFinished:
            logger.info("run %s 本轮结束（已暂停或到达终态）", runId)
        except LeaseLostError as error:
            await self._onLeaseLost(runId, workerId, error)
        except OrchestrationPauseError as error:
            await self._onPause(runId, workerId, error)
        except Exception as error:
            await self._onUnexpected(runId, workerId, error)

    # ------------------------------------------------------------------
    # 驱动循环
    # ------------------------------------------------------------------

    async def _drive(self, runId: int, workerId: str) -> None:
        while True:
            now = self._clock()
            run = await self._reader.loadRun(runId)
            if run is None:
                raise LeaseLostError(LOST_RUN_MISSING)
            await self._safetyPoint(run, workerId, now)
            steps = await self._reader.loadSteps(runId)
            if await self._resumePersisted(run, steps, workerId, now):
                continue
            await self._planningRound(
                run,
                steps,
                workerId,
                self._trailingToolFailures(steps),
            )

    async def _safetyPoint(
        self, run: AgentOrchestrationRun, workerId: str, now: datetime
    ) -> None:
        async with self._sessionFactory() as session:
            await self._store.assertExecutionAllowed(session, run, workerId, now)
        async with self._sessionFactory() as session:
            renewed = await self._store.renewLease(
                session,
                run.id,
                workerId,
                int(getattr(self._settings, "agentOrchestrationLeaseSeconds", 60)),
                now,
            )
        if not renewed:
            raise LeaseLostError("lease_expired")

    # ------------------------------------------------------------------
    # 恢复分派（唯一事实 = 已提交步骤）
    # ------------------------------------------------------------------

    async def _resumePersisted(
        self,
        run: AgentOrchestrationRun,
        steps: Sequence[AgentOrchestrationStep],
        workerId: str,
        now: datetime,
    ) -> bool:
        if not steps:
            return False
        last = steps[-1]
        if last.kind == StepKind.TOOL.value:
            if last.status == StepStatus.RUNNING.value:
                await self._replayTool(run, last, workerId, now)
                return True
            # 崩溃可能发生在「TOOL FAILED 提交」与「暂停写入」之间：阈值判定
            # 必须能从已提交的 TOOL 行重建，否则恢复后会多调一次 Planner，
            # 甚至因为下一次工具成功而悄悄越过应该暂停的失败次数。
            if self._trailingToolFailures(steps) >= self._maxToolFailures():
                raise OrchestrationPauseError(
                    PauseCode.TOOL_FAILED.value, MSG_TOOL_FAILED
                )
            return False
        if last.kind != StepKind.PLAN.value:
            return False
        if last.status == StepStatus.RUNNING.value:
            # 崩溃在规划中：复用同一行，iteration 不重复计数。
            await self._planningRound(
                run, steps, workerId, self._trailingToolFailures(steps), planStep=last
            )
            return True
        if last.status == StepStatus.SUCCEEDED.value:
            await self._resumeCommittedPlan(run, last, workerId, now)
            return True
        return False

    async def _resumeCommittedPlan(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        workerId: str,
        now: datetime,
    ) -> None:
        """已提交 PLAN 的直接收敛：CALL_TOOL 重建动作，FINISH/ASK_USER 不再规划。"""
        action = planStep.action
        if action == CALL_TOOL:
            await self._recoverTool(run, planStep, workerId, now)
            return
        if action == FINISH:
            await self._succeed(run, planStep.answer or "", workerId)
            return
        if action == ASK_USER:
            # 恢复路径：planStep 是刚从库里读出的已提交行，理由已持久化。
            await self._pauseForUser(
                run, planStep, workerId, planStep.plan_summary or ""
            )
            return
        raise RuntimeError(f"persisted PLAN action {action!r} cannot be resumed")

    async def _recoverTool(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        workerId: str,
        now: datetime,
    ) -> None:
        action = CallToolAction(
            toolName=planStep.tool_name or "",
            args=dict(planStep.input_json or {}),
            summary=planStep.plan_summary or "",
        )
        try:
            prepared = await self._prepareWithInfraRetry(run, action, workerId, now)
        except PlannerProtocolError as error:
            # 提交后工具 schema 被改动：旧参数已不可能通过，重新规划而不是死循环。
            logger.warning("run %s 已提交动作的参数不再合法：%s", run.id, error)
            await self._pauseConsumingPlan(
                run, planStep, workerId, PauseCode.PLANNER_OUTPUT_INVALID.value
            )
            return
        config = await self._requireModelConfig(run)
        await self._executeTool(
            run,
            planStep,
            action,
            prepared,
            config,
            workerId,
            self._trailingToolFailures(await self._reader.loadSteps(run.id)),
        )

    async def _replayTool(
        self,
        run: AgentOrchestrationRun,
        toolStep: AgentOrchestrationStep,
        workerId: str,
        now: datetime,
    ) -> None:
        """崩溃在工具执行期间：用 TOOL 行里的原始参数重放，行本身复用。"""
        action = CallToolAction(
            toolName=toolStep.tool_name or "",
            args=dict(toolStep.input_json or {}),
            summary="",
        )
        prepared = await self._prepareWithInfraRetry(run, action, workerId, now)
        config = await self._requireModelConfig(run)
        await self._executeTool(
            run,
            toolStep,
            action,
            prepared,
            config,
            workerId,
            self._trailingToolFailures(await self._reader.loadSteps(run.id)),
        )

    # ------------------------------------------------------------------
    # 规划轮
    # ------------------------------------------------------------------

    async def _planningRound(
        self,
        run: AgentOrchestrationRun,
        steps: Sequence[AgentOrchestrationStep],
        workerId: str,
        consecutiveFailures: int,
        *,
        planStep: AgentOrchestrationStep | None = None,
    ) -> None:
        now = self._clock()
        if planStep is None:
            async with self._sessionFactory() as session:
                planStep = await self._store.getOrCreatePlanStep(
                    session, run, workerId, now
                )
        config = await self._requireModelConfig(run)
        metered = MeteredStep(
            self._meteredFactory,
            self._sessionFactory,
            run.id,
            planStep.id,
            "PLANNER",
            config,
        )
        try:
            await self._planUntilAction(
                run, steps, planStep, config, metered, workerId, consecutiveFailures
            )
        finally:
            await metered.drain()

    async def _planUntilAction(
        self,
        run: AgentOrchestrationRun,
        steps: Sequence[AgentOrchestrationStep],
        planStep: AgentOrchestrationStep,
        config: Any,
        metered: MeteredStep,
        workerId: str,
        consecutiveFailures: int,
    ) -> None:
        retry = RetryBudget(
            int(getattr(self._settings, "agentOrchestrationMaxPlannerCorrections", 2))
        )
        infraFailures = 0
        while True:
            try:
                action = await self._planOnce(
                    run, steps, metered, retry.correction
                )
            except PlannerProtocolError as error:
                if not retry.recordProtocolFailure(error):
                    raise OrchestrationPauseError(
                        PauseCode.PLANNER_OUTPUT_INVALID.value, MSG_PLANNER_INVALID
                    ) from error
                continue
            except INFRASTRUCTURE_EXCEPTIONS as error:
                # 基础设施重试额度按阶段局部计数：Planner 侧与 prepare 侧各自
                # 有界，避免一次瞬时抖动耗尽另一阶段的机会。
                infraFailures += 1
                if infraFailures > INFRASTRUCTURE_RETRY_LIMIT:
                    raise OrchestrationPauseError(
                        PauseCode.INFRASTRUCTURE_ERROR.value, MSG_INFRASTRUCTURE_ERROR
                    ) from error
                logger.warning("run %s Planner 调用失败，重试：%s", run.id, error)
                continue

            if isinstance(action, CallToolAction):
                prepared = await self._prepareOrCorrect(
                    run, planStep, action, retry, workerId
                )
                if prepared is None:
                    continue
                await self._completePlan(run, planStep, action, workerId)
                await self._executeTool(
                    run,
                    planStep,
                    action,
                    prepared,
                    config,
                    workerId,
                    consecutiveFailures,
                )
                return
            if isinstance(action, AskUserAction):
                await self._completePlan(run, planStep, action, workerId)
                # 暂停码必须来自**手中的动作**：planStep 是提交前读出的陈旧
                # 对象，真实 store 在新会话提交，读它的 plan_summary 永远为 None。
                await self._pauseForUser(run, planStep, workerId, action.reason)
                return
            if isinstance(action, FinishAction):
                # FINISH 在任何 post-response 预算检查之前处理：本次合法完成的
                # 调用即使把成本推过上限，也应当成功结束。
                await self._completePlan(run, planStep, action, workerId)
                await self._succeed(run, action.answer, workerId)
                return
            raise RuntimeError("planner returned an unsupported action")

    async def _planOnce(
        self,
        run: AgentOrchestrationRun,
        steps: Sequence[AgentOrchestrationStep],
        metered: MeteredStep,
        correction: str | None,
    ) -> PlannerAction:
        client = metered(None)
        try:
            return await self._planner.plan(run, steps, client, correction)
        finally:
            # 每次 Planner 调用后立刻 drain：下一次 prepare 的成本预算必须
            # 看到这次调用的真实花费。
            await metered.drain()

    async def _prepareOrCorrect(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        action: CallToolAction,
        retry: RetryBudget,
        workerId: str,
    ) -> Any | None:
        """prepare 成功返回 PreparedTool；可纠正/可重试时返回 None 让循环继续。

        基础设施故障只重试 **prepare 本身**（同一动作），不重新调用 Planner：
        重放同一个已决定的动作才是正确的重试，而不是让模型在依赖抖动时
        再掷一次骰子。
        """
        now = self._clock()
        for attempt in range(INFRASTRUCTURE_RETRY_LIMIT + 1):
            try:
                return await self._prepare(run, action, workerId, now)
            except PlannerProtocolError as error:
                if not retry.recordProtocolFailure(error):
                    raise OrchestrationPauseError(
                        PauseCode.PLANNER_OUTPUT_INVALID.value, MSG_PLANNER_INVALID
                    ) from error
                return None
            except OrchestrationPauseError:
                # 权限拒绝/Agent 不可用是确定性结论：先把已生效的动作提交到
                # PLAN，恢复时可直接重建，无需再次调用 Planner。
                await self._completePlan(run, planStep, action, workerId)
                raise
            except INFRASTRUCTURE_EXCEPTIONS as error:
                if attempt >= INFRASTRUCTURE_RETRY_LIMIT:
                    raise OrchestrationPauseError(
                        PauseCode.INFRASTRUCTURE_ERROR.value,
                        MSG_INFRASTRUCTURE_ERROR,
                    ) from error
                logger.warning("run %s 工具 prepare 失败，重试：%s", run.id, error)
        raise OrchestrationPauseError(
            PauseCode.INFRASTRUCTURE_ERROR.value, MSG_INFRASTRUCTURE_ERROR
        )

    # ------------------------------------------------------------------
    # 单步执行
    # ------------------------------------------------------------------

    async def _prepare(
        self,
        run: AgentOrchestrationRun,
        action: CallToolAction,
        workerId: str,
        now: datetime,
    ) -> Any:
        async with self._sessionFactory() as session:
            return await self._executor.prepare(
                session, run, action.toolName, action.args, workerId, now
            )

    async def _prepareWithInfraRetry(
        self,
        run: AgentOrchestrationRun,
        action: CallToolAction,
        workerId: str,
        now: datetime,
    ) -> Any:
        for attempt in range(INFRASTRUCTURE_RETRY_LIMIT + 1):
            try:
                return await self._prepare(run, action, workerId, now)
            except PlannerProtocolError:
                raise
            except OrchestrationPauseError:
                raise
            except INFRASTRUCTURE_EXCEPTIONS as error:
                if attempt >= INFRASTRUCTURE_RETRY_LIMIT:
                    raise OrchestrationPauseError(
                        PauseCode.INFRASTRUCTURE_ERROR.value, MSG_INFRASTRUCTURE_ERROR
                    ) from error
                logger.warning("run %s 工具 prepare 失败，重试：%s", run.id, error)
        raise OrchestrationPauseError(
            PauseCode.INFRASTRUCTURE_ERROR.value, MSG_INFRASTRUCTURE_ERROR
        )

    async def _executeTool(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        action: CallToolAction,
        prepared: Any,
        config: Any,
        workerId: str,
        consecutiveFailures: int,
    ) -> None:
        now = self._clock()
        async with self._sessionFactory() as session:
            toolStep = await self._store.getOrCreateToolStep(
                session, run, planStep, workerId, now, action.toolName, prepared.args
            )
        metered = MeteredStep(
            self._meteredFactory,
            self._sessionFactory,
            run.id,
            toolStep.id,
            "TOOL",
            config,
        )
        context = AgentToolContext(
            llm_factory=metered, llm_config=config, actor=run.created_by
        )
        try:
            async with self._sessionFactory() as session:
                result = await self._executor.invoke(session, prepared, context)
        except Exception as error:
            await metered.drain()
            await self._recordToolFailure(run, toolStep, workerId, error)
            if consecutiveFailures + 1 >= self._maxToolFailures():
                raise OrchestrationPauseError(
                    PauseCode.TOOL_FAILED.value, MSG_TOOL_FAILED
                ) from error
            return
        await metered.drain()
        observation = serializeObservation(result, run.max_observation_chars)
        async with self._sessionFactory() as session:
            await self._store.completeToolStep(
                session,
                run.id,
                toolStep.id,
                workerId,
                self._clock(),
                status=StepStatus.SUCCEEDED.value,
                output=observation,
            )

    async def _recordToolFailure(
        self,
        run: AgentOrchestrationRun,
        toolStep: AgentOrchestrationStep,
        workerId: str,
        error: Exception,
    ) -> None:
        """失败观测：只落清洗后的错误码/文案，异常细节仅进服务端日志。"""
        logger.error(
            "run %s 工具 %s 执行失败", run.id, toolStep.tool_name, exc_info=True
        )
        observation = serializeObservation(
            ToolResult(data={"errorCode": ERROR_TOOL_EXECUTION}, answer=MSG_TOOL_FAILED),
            run.max_observation_chars,
        )
        async with self._sessionFactory() as session:
            await self._store.completeToolStep(
                session,
                run.id,
                toolStep.id,
                workerId,
                self._clock(),
                status=StepStatus.FAILED.value,
                output=observation,
                errorCode=ERROR_TOOL_EXECUTION,
                errorMessage=MSG_TOOL_FAILED,
            )

    # ------------------------------------------------------------------
    # 终态 / 暂停落库
    # ------------------------------------------------------------------

    async def _completePlan(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        action: PlannerAction,
        workerId: str,
    ) -> None:
        async with self._sessionFactory() as session:
            await self._store.completePlanStep(
                session, run.id, planStep.id, workerId, self._clock(), action
            )

    async def _succeed(
        self, run: AgentOrchestrationRun, answer: str, workerId: str
    ) -> None:
        async with self._sessionFactory() as session:
            await self._store.succeedRun(
                session, run.id, workerId, answer, self._clock()
            )
        raise RunFinished

    async def _pauseForUser(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        workerId: str,
        reason: str,
    ) -> None:
        code = (
            PauseCode.AMBIGUOUS.value
            if "ambiguous" in (reason or "").lower()
            else PauseCode.NEEDS_INPUT.value
        )
        await self._pauseConsumingPlan(run, planStep, workerId, code)

    async def _pauseConsumingPlan(
        self,
        run: AgentOrchestrationRun,
        planStep: AgentOrchestrationStep,
        workerId: str,
        code: str,
    ) -> None:
        async with self._sessionFactory() as session:
            await self._store.pauseRun(
                session,
                run.id,
                workerId,
                code,
                None,
                self._clock(),
                consumedStepId=planStep.id,
            )
        raise RunFinished

    # ------------------------------------------------------------------
    # 异常收尾
    # ------------------------------------------------------------------

    async def _onLeaseLost(
        self, runId: int, workerId: str, error: LeaseLostError
    ) -> None:
        if error.reason != LOST_CANCELLED:
            logger.info("run %s 停止推进：租约已失效（%s）", runId, error.reason)
            return
        try:
            async with self._sessionFactory() as session:
                await self._store.cancelRun(
                    session, runId, workerId, self._clock()
                )
            logger.info("run %s 已按取消请求收尾", runId)
        except Exception:
            logger.exception("run %s 取消收尾失败，等待租约过期后重试", runId)

    async def _onPause(
        self, runId: int, workerId: str, error: OrchestrationPauseError
    ) -> None:
        # TOOL_FAILED 的暂停必须连同失败行一起消费：阈值判定只依赖已提交
        # 步骤行，崩溃恢复时才能区分「暂停还没写」与「用户已经恢复」。不消费
        # 的话，恢复后字节级一致的 step 行会让阈值判定再次触发，运行无法恢复。
        consumedIds: list[int] | None = None
        if error.code == PauseCode.TOOL_FAILED.value:
            steps = await self._reader.loadSteps(runId)
            consumedIds = [step.id for step in self._trailingFailedToolSteps(steps)]
        try:
            async with self._sessionFactory() as session:
                await self._store.pauseRun(
                    session,
                    runId,
                    workerId,
                    error.code,
                    error.detail or None,
                    self._clock(),
                    consumedToolFailureIds=consumedIds,
                )
            logger.info("run %s 暂停（%s）", runId, error.code)
        except LeaseLostError as lost:
            if lost.reason == LOST_CANCELLED:
                await self._onLeaseLost(runId, workerId, lost)
            else:
                logger.info("run %s 暂停未写入：租约已失效（%s）", runId, lost.reason)
        except Exception:
            logger.exception("run %s 暂停写入失败", runId)

    async def _onUnexpected(
        self, runId: int, workerId: str, error: Exception
    ) -> None:
        if isInfrastructureError(error):
            # 外部依赖故障：不写终态，交给租约过期后的新 worker 重试。
            logger.error(
                "run %s 依赖服务异常，本轮停止（等待租约过期重试）",
                runId,
                exc_info=True,
            )
            return
        logger.error("run %s 遇到不变量错误，标记 FAILED", runId, exc_info=True)
        try:
            async with self._sessionFactory() as session:
                await self._store.failRun(
                    session,
                    runId,
                    workerId,
                    ERROR_INTERNAL,
                    MSG_INTERNAL_ERROR,
                    self._clock(),
                )
        except Exception:
            logger.exception("run %s 写入 FAILED 也失败，等待租约过期", runId)

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------

    async def _requireModelConfig(self, run: AgentOrchestrationRun) -> Any:
        config = await self._reader.loadModelConfig(run)
        if config is None or not bool(getattr(config, "is_active", False)):
            raise OrchestrationPauseError(
                PauseCode.INFRASTRUCTURE_ERROR.value, MSG_MODEL_UNAVAILABLE
            )
        return config

    def _maxToolFailures(self) -> int:
        value = int(getattr(self._settings, "agentOrchestrationMaxToolFailures", 2))
        return max(1, value)

    @staticmethod
    def _trailingFailedToolSteps(
        steps: Sequence[AgentOrchestrationStep],
    ) -> list[AgentOrchestrationStep]:
        """尾部连续失败（非 FAILED 的 TOOL 行即终止）的 TOOL 行，最新在前。"""
        failed: list[AgentOrchestrationStep] = []
        for step in reversed(steps):
            if step.kind != StepKind.TOOL.value:
                continue
            if step.status != StepStatus.FAILED.value:
                break
            failed.append(step)
        return failed

    @staticmethod
    def _trailingToolFailures(steps: Sequence[AgentOrchestrationStep]) -> int:
        """从持久化步骤推导「连续工具失败次数」——崩溃后仍然成立。"""
        return len(AgentOrchestrationRunner._trailingFailedToolSteps(steps))
