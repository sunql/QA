"""P5 Command 执行器（执行 / 重试 / 补偿 / 失败隔离）。

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
   实例 COMPENSATED（先 FAILED 再 COMPENSATED，状态机只允许这一条路径）。
   适配器不支持补偿 ⇒ 跳过并**留痕**（不假装补偿过）。

⚠️ **P5 表写入的唯一入口**是存储层（``workflow_store.py``）⇒ 其它模块直接改
``workflow_*`` / ``business_command`` / ``approval_task`` 会被源码级反向守卫判红
（``app/tests/unit/test_workflow_write_gate.py``）。

⚠️ **守卫钉不住的部分（别当成事实）**：守卫只管**库表**写入，管不了「外部系统
写」——适配器注册表是**开放**的，任何模块都能 ``register`` 一个直写外部系统的
handler ⇒「所有外部写都经 Command」是**约定 + 评审**，不是守卫能证明的事实。
⇒ 注册点已收口到**组装根**（``app/workers/workflow_worker.py``）：新增适配器必须
改到那里 ⇒ 必然过评审。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.workflow_models import BusinessCommand, WorkflowInstance, WorkflowStep
from app.services.workflow_audit_service import WorkflowAuditService
from app.services.workflow_command_ports import (
    CommandExecutionError,
    CommandHandlerRegistry,
    UnknownCommandTypeError,
    isCompensable,
)
from app.services.workflow_orchestrator import WorkflowOrchestrator
from app.services.workflow_state_machine import isLiveInstance
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

# 补偿是**系统事件**（执行失败触发的自动回滚）⇒ 默认 actor 用系统标识。
SYSTEM_ACTOR_COMPENSATION = "system:command-compensation"

STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_ABORTED = "ABORTED"
STATUS_SKIPPED = "SKIPPED"
STATUS_COMPENSATED = "COMPENSATED"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXECUTING = "EXECUTING"
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"



@dataclass(frozen=True)
class CommandOutcome:
    """一次命令执行的结果（不可变）。"""

    commandId: int
    status: str
    attempts: int
    result: dict[str, Any] | None = None
    errorMessage: str | None = None
    isTerminal: bool = False


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


@dataclass(frozen=True)
class _CompensationSummary:
    """一轮补偿的统计（计数在**这里定格** ⇒ 结果可复现）。"""

    outcomes: tuple[CommandOutcome, ...]
    compensatedStepIds: frozenset[int]
    succeeded: int
    failed: int
    skipped: int

    @property
    def total(self) -> int:
        """本轮**确实尝试过**的补偿条数（0 ⇒ 不留痕，不假称「已补偿」）。"""

        return self.succeeded + self.failed + self.skipped


class CommandExecutor:
    """业务命令执行器（核心）。"""

    def __init__(
        self,
        registry: CommandHandlerRegistry,
        *,
        maxAttempts: int = 3,
        compensateOnFailure: bool = True,
        audit: WorkflowAuditService | None = None,
        orchestrator: WorkflowOrchestrator | None = None,
    ) -> None:
        self._registry = registry
        self._maxAttempts = maxAttempts
        self._compensateOnFailure = compensateOnFailure
        self._store = WorkflowStore()
        self._audit = audit or WorkflowAuditService()
        self._orchestrator = orchestrator or WorkflowOrchestrator()

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
            result = await handler.execute(session, command=command)
            return _HandlerRun(result=result)
        except UnknownCommandTypeError as exc:
            return _HandlerRun(
                failure=await self._failureFor(
                    session, ctx=ctx, message=str(exc), retryable=False, actor=workerId
                )
            )
        except CommandExecutionError as exc:
            return _HandlerRun(
                failure=await self._failureFor(
                    session,
                    ctx=ctx,
                    message=exc.message,
                    retryable=exc.retryable,
                    actor=workerId,
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 失败隔离：兜住一切适配器异常
            logger.exception(
                "命令执行异常（已收敛为失败）：commandId=%s type=%s",
                ctx.commandId,
                ctx.commandType,
            )
            return _HandlerRun(
                failure=await self._failureFor(
                    session,
                    ctx=ctx,
                    message=f"{type(exc).__name__}: {exc}",
                    retryable=True,
                    actor=workerId,
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
    ) -> CommandOutcome:
        """失败收敛：回滚半写副作用 → 显式落失败状态 + 次数 → 判定是否终态。

        ⚠️ **终态必须落库**：终态此前只存在于内存（``outcome.isTerminal``）⇒
        库里仍是 ``FAILED`` 且 ``attempts < maxAttempts`` ⇒ Worker 把它当
        「可重试失败」再领一次 ⇒ 业务上不可重试的命令被无限重试。
        ⇒ 终态失败落 ``ABORTED``（终态状态，Worker 领不走）。
        """

        await session.rollback()

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
                reason=f"命令成功后推进失败：{type(exc).__name__}: {exc}",
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
        """补偿同实例内**已成功**的命令（逆序），并把步骤/实例收敛到 COMPENSATED。

        补偿是**尽力而为**：某条补偿失败**不影响**其余补偿（失败隔离），但会在
        结果里留痕（status=FAILED + 错误信息）。

        ⚠️ **待执行命令一并取消**（否则「回滚」只是纸面动作 —— Worker 照样领走
        PENDING 命令去写外部系统）。
        ⚠️ **锁序**：命令 → 实例 → 步骤（与取消路径不互锁 ⇒ 不会 40P01）。
        ⚠️ **失败隔离用 SAVEPOINT**：整事务 ``rollback()`` 会把「已取消的待执行
        命令」和「已完成的补偿」一起撤销 ⇒ 每条补偿一个 SAVEPOINT，失败只回滚那条。
        """

        await self._store.cancelPendingCommands(
            session,
            instanceId=instanceId,
            reason=f"实例已补偿，待执行命令不再执行：{reason}",
        )

        targets = await self._loadCompensationTargets(session, instanceId)
        summary = await self._compensateAll(session, targets)

        # 收敛实例（命令 → 实例）
        await self._compensateInstance(session, instanceId, reason=reason)
        # 收敛步骤（命令 → 实例 → 步骤）
        for stepId in sorted(summary.compensatedStepIds):
            await self._compensateStep(session, stepId)

        # 留痕：**只在确实尝试过补偿时**写（不假称「已补偿」），并带上计数。
        if summary.total > 0:
            await self._recordCompensation(
                session,
                instanceId=instanceId,
                reason=reason,
                actor=actor,
                succeededCount=summary.succeeded,
                failedCount=summary.failed,
                skippedCount=summary.skipped,
            )
        await session.commit()

        return list(summary.outcomes)

    async def _compensateAll(
        self, session: AsyncSession, targets: Sequence[tuple[int, str, int | None, int]]
    ) -> _CompensationSummary:
        """逐条补偿并统计（失败隔离：单条失败不影响其余）。"""

        outcomes: list[CommandOutcome] = []
        compensatedStepIds: set[int] = set()
        succeeded = 0
        failed = 0
        skipped = 0

        for commandId, commandType, stepId, attempts in targets:
            outcome = await self._compensateOne(
                session,
                commandId=commandId,
                commandType=commandType,
                attempts=attempts,
            )
            outcomes.append(outcome)
            if outcome.status == STATUS_COMPENSATED:
                succeeded += 1
                if stepId is not None:
                    compensatedStepIds.add(stepId)
            elif outcome.status == STATUS_SKIPPED:
                skipped += 1
            else:
                failed += 1

        return _CompensationSummary(
            outcomes=tuple(outcomes),
            compensatedStepIds=frozenset(compensatedStepIds),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )

    async def _loadCompensationTargets(
        self, session: AsyncSession, instanceId: int
    ) -> list[tuple[int, str, int | None, int]]:
        """取待补偿的命令（**已成功**的，逆序）。

        ⚠️ 先把标量取出来：SAVEPOINT 回滚会让 ORM 实例 expire ⇒ 回滚之后
        再访问 row.id / row.payload 会触发**同步懒加载** ⇒ MissingGreenlet。
        """

        rows = (
            await session.execute(
                select(BusinessCommand)
                .where(
                    BusinessCommand.instance_id == instanceId,
                    BusinessCommand.status == STATUS_SUCCEEDED,
                )
                .order_by(BusinessCommand.id.desc())
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        return [(row.id, row.command_type, row.step_id, row.attempts) for row in rows]

    async def _compensateOne(
        self,
        session: AsyncSession,
        *,
        commandId: int,
        commandType: str,
        attempts: int,
    ) -> CommandOutcome:
        """补偿**一条**命令（失败隔离：只回滚这一条 ⇒ SAVEPOINT）。

        ⚠️ **失败隔离用 SAVEPOINT**：整事务 ``rollback()`` 会把「已取消的待执行
        命令」和「已完成的补偿」一起撤销 ⇒ 实例却仍被标成 COMPENSATED ⇒
        补偿名存实亡（外部写照旧发生）。⇒ 每条补偿一个 SAVEPOINT，失败只回滚
        那一条。
        """

        handler = self._registry.resolveOptional(commandType)
        if handler is None or not isCompensable(handler):
            return CommandOutcome(
                commandId=commandId,
                status=STATUS_SKIPPED,
                attempts=attempts,
                errorMessage="适配器不支持补偿（跳过，不假称已补偿）",
            )

        try:
            # 重读（刷新）⇒ 上一次 SAVEPOINT 回滚导致的 expire 不会传染到这条。
            row = await self._reloadCommand(session, commandId)
            async with session.begin_nested():
                result = await handler.compensate(session, command=row)
        except Exception as exc:  # noqa: BLE001 —— 补偿失败隔离
            logger.exception("补偿失败：commandId=%s type=%s", commandId, commandType)
            return CommandOutcome(
                commandId=commandId,
                status=STATUS_FAILED,
                attempts=attempts,
                errorMessage=f"补偿失败：{type(exc).__name__}: {exc}",
            )

        await self._store.completeCommand(session, commandId, STATUS_COMPENSATED, result=result)
        return CommandOutcome(
            commandId=commandId,
            status=STATUS_COMPENSATED,
            attempts=attempts,
            result=result,
            isTerminal=True,
        )

    async def _reloadCommand(
        self, session: AsyncSession, commandId: int
    ) -> BusinessCommand:
        """重读命令行（刷新属性）—— SAVEPOINT 回滚后的 ORM 实例可能是过期态。"""

        row = (
            await session.execute(
                select(BusinessCommand)
                .where(BusinessCommand.id == commandId)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"业务命令 {commandId} 不存在")
        return row

    async def _recordCompensation(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        reason: str,
        actor: str,
        succeededCount: int = 0,
        failedCount: int = 0,
        skippedCount: int = 0,
    ) -> None:
        """补偿留痕（治理审计：谁触发的补偿、为什么、补偿了几条）。"""

        instance = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if instance is None:
            return
        await self._audit.recordCompensation(
            session,
            instance=instance,
            actor=actor,
            reason=reason,
            succeededCount=succeededCount,
            failedCount=failedCount,
            skippedCount=skippedCount,
        )

    async def _compensateStep(self, session: AsyncSession, stepId: int) -> None:
        """步骤 → COMPENSATED（RUNNING 需先 FAILED：状态机不允许多跳）。"""

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
        if status == "SUCCEEDED":
            await self._store.advanceStep(session, stepId, STATUS_COMPENSATED)
            return
        if status == "RUNNING":
            await self._store.advanceStep(session, stepId, STATUS_FAILED)
            await self._store.advanceStep(session, stepId, STATUS_COMPENSATED)
            return
        if status == STATUS_FAILED:
            await self._store.advanceStep(session, stepId, STATUS_COMPENSATED)

    async def _compensateInstance(self, session: AsyncSession, instanceId: int, *, reason: str) -> None:
        """实例 → FAILED → COMPENSATED（状态机只允许 COMPENSATED 来自 FAILED / COMPLETED）。"""

        current = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            return

        status = current.status
        if status in {"PENDING", "RUNNING", "WAITING_APPROVAL", "APPROVED"}:
            await self._store.advanceInstance(session, instanceId, STATUS_FAILED, errorMessage=reason)
            status = STATUS_FAILED
        if status in {STATUS_FAILED, STATUS_COMPLETED}:
            await self._store.advanceInstance(session, instanceId, STATUS_COMPENSATED)
