"""P5 Command 执行器（执行 / 重试 / 失败隔离）。

W3 完成定义：「**注入失败后补偿执行；新适配器不改核心**」。本模块就是那个「核心」：
它只认 ``command_type`` → 适配器 的注册表，**不认任何具体业务** ⇒ 新增 ERP/SRM 适配器
只需注册一个 handler，本文件一行不改（D-8）。

四条纪律：

1. **原子执行**：claim（条件 UPDATE）→ 适配器执行 → 置终态 → **一个事务提交**。
   worker 中途死掉 ⇒ 事务回滚 ⇒ 命令回到 PENDING ⇒ 被别的 worker 重领
   （不留「卡在 EXECUTING 的僵尸命令」）。
2. **失败隔离**：适配器抛错**不会**打断调用方（worker 主循环）；异常被收敛成
   ``CommandOutcome``，失败详情落 ``business_command.error_message``。
3. **重试语义**：可重试失败 ⇒ 置 FAILED（仍可被再次领取）；尝试次数达上限 ⇒ 终态失败。
   不可重试失败（``retryable=False``）⇒ 直接终态。
4. **补偿钩子**：终态失败 ⇒ 补偿同实例内**已成功**的命令（逆序）⇒ 命令/步骤 COMPENSATED、
   实例 COMPENSATED。补偿实现见 ``workflow_compensation.py``（独立模块：它是另一条
   事务线，混进来会让本文件超长、也让两条锁序看不清）。

⚠️ **P5 表写入的唯一入口**是存储层（``workflow_store.py``）⇒ 其它模块直接改
``workflow_*`` / ``business_command`` / ``approval_task`` 会被源码级反向守卫判红
（``app/tests/unit/test_workflow_write_gate.py``）。

⚠️ **守卫钉不住的部分（别当成事实）**：守卫只管**库表**写入，管不了「外部系统
写」——适配器注册表是**开放**的，任何模块都能 ``register`` 一个直写外部系统的
handler ⇒「所有外部写都经 Command」是**约定 + 评审**，不是守卫能证明的事实。
⇒ 注册点已收口到**组装根**（``app/workers/workflow_worker.py``）：新增适配器必须
改到那里 ⇒ 必然过评审。

⚠️ **错误消息落库前必过净化**（``workflow_errors.py``）：适配器抛的消息可能带
凭据 / PII ⇒ 直接落库 = 凭据进库（不可逆）⇒ 落库前抹值、控长、压控制字符。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import BusinessCommand, WorkflowStep
from app.services.workflow_audit_service import WorkflowAuditService
from app.services.workflow_command_ports import (
    CommandExecutionError,
    CommandHandlerRegistry,
    UnknownCommandTypeError,
)
from app.services.workflow_command_types import (
    STATUS_ABORTED,
    STATUS_CANCELLED,
    STATUS_EXECUTING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCEEDED,
    CommandOutcome,
)
from app.services.workflow_compensation import (
    SYSTEM_ACTOR_COMPENSATION,
    CompensationRunner,
)
from app.services.workflow_errors import (
    ERROR_KIND_ADAPTER,
    ERROR_KIND_INFRA,
    describeError,
    sanitizeAndLabel,
    sanitizeErrorMessage,
)
from app.services.workflow_orchestrator import WorkflowOrchestrator
from app.services.workflow_state_machine import isLiveInstance
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

# 适配器调用的**默认超时**（秒）：没有超时 ⇒ 一个卡死的下游能把 Worker 的
# 那一轮永远占住（事务不提交 ⇒ 连接不释放 ⇒ 连接池耗尽 ⇒ 全部命令停摆）。
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0

__all__ = [
    "CommandExecutor",
    "CommandOutcome",
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "SYSTEM_ACTOR_COMPENSATION",
]


@dataclass(frozen=True)
class _HandlerRun:
    """适配器调用结果：**要么有结果，要么有失败**（不外泄异常）。"""

    result: dict[str, Any] | None = None
    failure: CommandOutcome | None = None


@dataclass(frozen=True)
class _CommandContext:
    """命令的**标量快照**。

    ⚠️ 为什么先取标量：``rollback()`` 会 expire 全部 ORM 实例 ⇒ 之后访问
    ``command.id`` / ``command.status`` 会触发**同步懒加载** ⇒ MissingGreenlet。
    """

    commandId: int
    instanceId: int
    commandType: str
    stepId: int | None
    attemptedCount: int

    @classmethod
    def fromCommand(cls, command: BusinessCommand) -> _CommandContext:
        return cls(
            commandId=command.id,
            instanceId=command.instance_id,
            commandType=command.command_type,
            stepId=command.step_id,
            attemptedCount=command.attempts,
        )


class CommandExecutor:
    """业务命令执行器（核心）。"""

    def __init__(
        self,
        registry: CommandHandlerRegistry,
        *,
        maxAttempts: int = 3,
        compensateOnFailure: bool = True,
        commandTimeoutSeconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        audit: WorkflowAuditService | None = None,
        orchestrator: WorkflowOrchestrator | None = None,
    ) -> None:
        self._registry = registry
        self._maxAttempts = maxAttempts
        self._compensateOnFailure = compensateOnFailure
        self._commandTimeoutSeconds = commandTimeoutSeconds
        self._store = WorkflowStore()
        self._audit = audit or WorkflowAuditService()
        self._orchestrator = orchestrator or WorkflowOrchestrator()
        self._compensation = CompensationRunner(
            registry,
            store=self._store,
            audit=self._audit,
        )

    # ---------------------------------------------------------------- 口径

    @property
    def maxAttempts(self) -> int:
        """最大尝试次数（**唯一出口**）。

        ⚠️ 为什么是 property：领取时的过滤条件（``attempts < maxAttempts``）与
        失败后的终态判定（``attempts >= maxAttempts``）**必须用同一个值** ⇒
        两个地方各读各的配置 ⇒ 改了一个忘了另一个 ⇒ 一条命令被永远重试或
        永远不重试。⇒ 调用方（Worker）一律从这里取。
        """

        return self._maxAttempts

    @property
    def commandTimeoutSeconds(self) -> float:
        """适配器调用的超时（秒）。"""

        return self._commandTimeoutSeconds

    # ---------------------------------------------------------------- 执行

    async def execute(self, session: AsyncSession, commandId: int, *, workerId: str) -> CommandOutcome:
        """执行一条命令（成功 / 可重试失败 / 终态失败）。

        **不抛异常**（失败隔离）：任何适配器异常都被收敛成 ``CommandOutcome``。
        """

        command = await self._store.claimCommand(
            session,
            commandId,
            workerId=workerId,
            maxAttempts=self._maxAttempts,
        )
        if command is None:
            return CommandOutcome(
                commandId=commandId,
                status=STATUS_SKIPPED,
                attempts=0,
                errorMessage="命令不可领取（已被领走 / 已终态 / 尝试次数耗尽）",
                isTerminal=True,
            )

        return await self.executeClaimed(session, command, workerId=workerId)

    async def executeClaimed(
        self,
        session: AsyncSession,
        command: BusinessCommand,
        *,
        workerId: str,
    ) -> CommandOutcome:
        """执行**已领取**的命令（调用方已完成条件 UPDATE claim）。

        Worker 用 ``claimNextCommand``（``SKIP LOCKED``）原子领走一条命令后交给
        本方法 ⇒ claim → 执行 → 终态仍在**同一事务**里（worker 中途死掉 ⇒
        命令回到 PENDING 被重领）。⚠️ 只有「本 worker 领的、EXECUTING」的命令才执行。
        """

        notOwned = self._rejectUnownedCommand(command, workerId)
        if notOwned is not None:
            return notOwned

        # ⚠️ rollback 会 expire 全部 ORM 实例 ⇒ 先取标量快照。
        ctx = _CommandContext.fromCommand(command)

        stopped = await self._rejectStoppedInstance(
            session,
            instanceId=ctx.instanceId,
            commandId=ctx.commandId,
            attemptedCount=ctx.attemptedCount,
        )
        if stopped is not None:
            return stopped

        run = await self._runHandler(session, command, ctx=ctx, workerId=workerId)
        if run.failure is not None:
            return run.failure

        await self._completeSuccessfully(
            session, commandId=ctx.commandId, stepId=ctx.stepId, result=run.result
        )

        # 推进工作流（编排器入队下一条命令 / 收尾实例）。
        # ⚠️ 驱动失败**不回滚已发生的外部写**（重试会重复写）⇒ 改为补偿实例。
        await self._driveNext(session, instanceId=ctx.instanceId, workerId=workerId)

        return CommandOutcome(
            commandId=ctx.commandId,
            status=STATUS_SUCCEEDED,
            attempts=ctx.attemptedCount,
            result=run.result,
            isTerminal=True,
        )

    async def _completeSuccessfully(
        self,
        session: AsyncSession,
        *,
        commandId: int,
        stepId: int | None,
        result: dict[str, Any] | None,
    ) -> None:
        """落命令终态 + 步骤成功，并在**同一事务**里提交（事实落库 ⇒ 原子）。

        （worker 中途死掉 ⇒ 一起回滚 ⇒ 命令回到 PENDING 被重领。）
        """

        await self._store.completeCommand(session, commandId, STATUS_SUCCEEDED, result=result)
        if stepId is not None:
            await self._advanceStepToSucceeded(session, stepId)
        await session.commit()

    def _rejectUnownedCommand(
        self, command: BusinessCommand, workerId: str
    ) -> CommandOutcome | None:
        """归属校验：只有「本 worker 领的、处于 EXECUTING」的命令才执行。

        返回 ``None`` 表示通过；否则返回**跳过**的结果（不执行）。
        """

        if command.status == STATUS_EXECUTING and command.executed_by == workerId:
            return None
        return CommandOutcome(
            commandId=command.id,
            status=STATUS_SKIPPED,
            attempts=command.attempts,
            errorMessage="命令未被本 worker 领取（状态/归属不符）",
            isTerminal=True,
        )

    async def _rejectStoppedInstance(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        commandId: int,
        attemptedCount: int,
    ) -> CommandOutcome | None:
        """预检：归属实例**还活着吗**？（取消 / 补偿 / 完成之后不得再写外部系统）。

        ⚠️ 这里刻意**不加行锁**：Worker 的锁序是「命令 → 实例」，若这里再锁
        实例，就与取消路径的「命令 → 实例」之外的顺序打架 ⇒ 40P01 死锁。
        取消路径会先锁命令 ⇒ 命令被取消 ⇒ 本次 claim 命中 0 行 ⇒ 走不到这里。
        """

        instanceStatus = await self._store.getInstanceStatus(session, instanceId)
        if instanceStatus is not None and isLiveInstance(instanceStatus):
            return None

        await self._store.cancelCommand(
            session,
            commandId,
            reason=f"实例已停止（{instanceStatus or 'NOT_FOUND'}），命令不再执行",
        )
        await session.commit()
        return CommandOutcome(
            commandId=commandId,
            status=STATUS_CANCELLED,
            attempts=attemptedCount,
            errorMessage=f"实例已停止（{instanceStatus or 'NOT_FOUND'}），命令未执行",
            isTerminal=True,
        )

    async def _runHandler(
        self,
        session: AsyncSession,
        command: BusinessCommand,
        *,
        ctx: _CommandContext,
        workerId: str,
    ) -> _HandlerRun:
        """调用适配器：**任何异常都收敛成失败**（不外泄给 Worker 主循环）。"""

        try:
            handler = self._registry.resolve(ctx.commandType)
            # ⚠️ **SAVEPOINT**：适配器的半写副作用要回滚，但**claim 不能回滚**
            #   （claim = 归属 + 尝试次数 ⇒ 回滚了就是「没人执行过」的假象）。
            #   ⇒ 只回滚 SAVEPOINT 之内的那一段。
            async with session.begin_nested():
                result = await asyncio.wait_for(
                    handler.execute(session, command=command),
                    timeout=self._commandTimeoutSeconds,
                )
            return _HandlerRun(result=result)
        except UnknownCommandTypeError as exc:
            return _HandlerRun(
                failure=await self._failureFor(
                    session,
                    ctx=ctx,
                    message=sanitizeAndLabel(str(exc), ERROR_KIND_ADAPTER),
                    retryable=False,
                    actor=workerId,
                    kind=ERROR_KIND_ADAPTER,
                )
            )
        except CommandExecutionError as exc:
            return _HandlerRun(
                failure=await self._failureFor(
                    session,
                    ctx=ctx,
                    message=sanitizeAndLabel(exc.message, ERROR_KIND_ADAPTER),
                    retryable=exc.retryable,
                    actor=workerId,
                    kind=ERROR_KIND_ADAPTER,
                )
            )
        except TimeoutError:
            # ⚠️ 适配器**超时** ⇒ 归类为**基础设施错误**（下游没应答）
            #   ⇒ 可重试（等下游恢复），且巡检时一眼能与「业务拒绝」区分。
            logger.warning(
                "命令执行超时（已收敛为失败）：commandId=%s type=%s timeout=%.1fs",
                ctx.commandId,
                ctx.commandType,
                self._commandTimeoutSeconds,
            )
            return _HandlerRun(
                failure=await self._failureFor(
                    session,
                    ctx=ctx,
                    message=sanitizeAndLabel(
                        f"适配器调用超过 {self._commandTimeoutSeconds:.0f} 秒未应答",
                        ERROR_KIND_INFRA,
                    ),
                    retryable=True,
                    actor=workerId,
                    kind=ERROR_KIND_INFRA,
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 失败隔离：兜住一切适配器异常
            logger.exception(
                "命令执行异常（已收敛为失败）：commandId=%s type=%s",
                ctx.commandId,
                ctx.commandType,
            )
            kind, message = describeError(exc)
            return _HandlerRun(
                failure=await self._failureFor(
                    session,
                    ctx=ctx,
                    message=message,
                    retryable=True,
                    actor=workerId,
                    kind=kind,
                )
            )

    async def _failureFor(
        self,
        session: AsyncSession,
        *,
        ctx: _CommandContext,
        message: str,
        retryable: bool,
        actor: str,
        kind: str = ERROR_KIND_ADAPTER,
    ) -> CommandOutcome:
        """把适配器异常收敛成失败结果（落库 + 判定是否终态）。"""

        return await self._handleFailure(
            session,
            commandId=ctx.commandId,
            instanceId=ctx.instanceId,
            attemptedCount=ctx.attemptedCount,
            message=message,
            retryable=retryable,
            actor=actor,
            kind=kind,
        )

    async def _handleFailure(
        self,
        session: AsyncSession,
        *,
        commandId: int,
        instanceId: int,
        attemptedCount: int,
        message: str,
        retryable: bool,
        actor: str = SYSTEM_ACTOR_COMPENSATION,
        kind: str = ERROR_KIND_ADAPTER,
    ) -> CommandOutcome:
        """失败收敛：显式落失败状态 + 次数 → 判定是否终态。

        ⚠️ **终态必须落库**：终态此前只存在于内存（``outcome.isTerminal``）⇒
        库里仍是 ``FAILED`` 且 ``attempts < maxAttempts`` ⇒ Worker 把它当
        「可重试失败」再领一次 ⇒ 业务上不可重试的命令被无限重试。
        ⇒ 终态失败落 ``ABORTED``（终态状态，Worker 领不走）。

        ⚠️ **这里不再整事务回滚**：回滚会把 claim（``EXECUTING`` + 归属 +
        ``attempts+1``）一并撤销 ⇒ ① 命令在回滚与落失败之间短暂回到 ``PENDING``
        （可被别的 worker 领走）；② 「谁执行的」丢失（``executed_by`` 变 NULL）。
        ⇒ 适配器的半写副作用改由 **SAVEPOINT** 回滚（见 ``_runHandler``），
        claim 留在事务里与失败终态**一起提交**（事实与归属同生共死）。
        """

        isTerminal = (not retryable) or attemptedCount >= self._maxAttempts

        await self._store.failCommand(
            session,
            commandId,
            attempts=attemptedCount,
            errorMessage=message,
            terminal=isTerminal,
        )
        await session.commit()

        if isTerminal and self._compensateOnFailure:
            await self.compensateInstance(session, instanceId, reason=message, actor=actor)

        return CommandOutcome(
            commandId=commandId,
            status=STATUS_ABORTED if isTerminal else STATUS_FAILED,
            attempts=attemptedCount,
            errorMessage=message,
            isTerminal=isTerminal,
            errorKind=kind,
        )

    # ---------------------------------------------------------------- 推进

    async def _driveNext(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        workerId: str,
    ) -> None:
        """命令成功后驱动工作流前进（编排器入队下一条命令 / 收尾实例）。

        驱动失败（定义写错 / 审批矩阵失效）⇒ **不回滚已发生的外部写**
        （回滚后重领 ⇒ 重复写）⇒ 改为**补偿实例**：把已成功的命令回滚，
        实例收敛到 COMPENSATED（失败隔离：驱动异常不冒泡给 Worker 主循环）。
        """

        try:
            await self._orchestrator.drive(session, instanceId=instanceId)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 —— 驱动失败隔离
            logger.exception(
                "命令成功后推进工作流失败 ⇒ 补偿实例：instanceId=%s", instanceId
            )
            await session.rollback()
            await self.compensateInstance(
                session,
                instanceId,
                reason=(
                    f"命令成功后推进失败：{type(exc).__name__}: "
                    f"{sanitizeErrorMessage(str(exc))}"
                ),
                actor=workerId,
            )

    async def _advanceStepToSucceeded(self, session: AsyncSession, stepId: int) -> None:
        """步骤 → SUCCEEDED（PENDING 先补一跳 RUNNING：状态机不允许多跳）。"""

        current = (
            await session.execute(
                select(WorkflowStep)
                .where(WorkflowStep.id == stepId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            return

        status = current.status
        if status == STATUS_PENDING:
            await self._store.advanceStep(session, stepId, STATUS_RUNNING)
            status = STATUS_RUNNING
        if status == STATUS_RUNNING:
            await self._store.advanceStep(session, stepId, STATUS_SUCCEEDED)

    # ---------------------------------------------------------------- 补偿

    async def compensateInstance(
        self,
        session: AsyncSession,
        instanceId: int,
        *,
        reason: str,
        actor: str = SYSTEM_ACTOR_COMPENSATION,
    ) -> list[CommandOutcome]:
        """补偿同实例内**已成功**的命令（逆序），并把步骤 / 实例收敛到 COMPENSATED。

        实现见 ``workflow_compensation.py``（独立模块：补偿是另一条事务线）。
        """

        return await self._compensation.compensateInstance(
            session,
            instanceId,
            reason=reason,
            actor=actor,
        )
