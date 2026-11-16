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

⚠️ 写操作的**唯一**入口是本模块 + 其适配器层；其它服务层不得绕过它直接写外部系统
（G6 判据，由源码级反向守卫钉住）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import BusinessCommand, WorkflowInstance, WorkflowStep
from app.services.workflow_audit_service import WorkflowAuditService
from app.services.workflow_command_ports import (
    CommandExecutionError,
    CommandHandlerRegistry,
    UnknownCommandTypeError,
    isCompensable,
)
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

# 补偿是**系统事件**（执行失败触发的自动回滚）⇒ 默认 actor 用系统标识。
SYSTEM_ACTOR_COMPENSATION = "system:command-compensation"

STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_COMPENSATED = "COMPENSATED"
STATUS_EXECUTING = "EXECUTING"
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"

# 步骤「已完成」集合（全部落在集合里 ⇒ 实例可置 COMPLETED）。
_STEP_DONE_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_SKIPPED})
# 实例允许置 COMPLETED 的来源状态（其它状态说明还在等审批 / 已终态）。
_INSTANCE_COMPLETABLE_STATUSES = frozenset({STATUS_RUNNING, "APPROVED"})


@dataclass(frozen=True)
class CommandOutcome:
    """一次命令执行的结果（不可变）。"""

    commandId: int
    status: str
    attempts: int
    result: dict[str, Any] | None = None
    errorMessage: str | None = None
    isTerminal: bool = False


class CommandExecutor:
    """业务命令执行器（核心）。"""

    def __init__(
        self,
        registry: CommandHandlerRegistry,
        *,
        maxAttempts: int = 3,
        compensateOnFailure: bool = True,
        audit: WorkflowAuditService | None = None,
    ) -> None:
        self._registry = registry
        self._maxAttempts = maxAttempts
        self._compensateOnFailure = compensateOnFailure
        self._store = WorkflowStore()
        self._audit = audit or WorkflowAuditService()

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

        Worker 走这条路：它用 ``claimNextCommand``（``SKIP LOCKED``）原子领走
        一条命令后交给本方法 ⇒ claim → 执行 → 终态仍在**同一个事务**里
        （worker 中途死掉 ⇒ 事务回滚 ⇒ 命令回到 PENDING ⇒ 被别的 worker 重领）。

        ⚠️ 归属校验：只有「本 worker 领的、处于 EXECUTING」的命令才执行
        （否则等于给未领取的命令开写入口）。
        """

        if command.status != STATUS_EXECUTING or command.executed_by != workerId:
            return CommandOutcome(
                commandId=command.id,
                status=STATUS_SKIPPED,
                attempts=command.attempts,
                errorMessage="命令未被本 worker 领取（状态/归属不符）",
                isTerminal=True,
            )

        # ⚠️ rollback 会 expire 全部 ORM 实例 ⇒ 先把需要的标量取出来。
        commandId = command.id
        instanceId = command.instance_id
        commandType = command.command_type
        stepId = command.step_id
        attemptedCount = command.attempts

        try:
            handler = self._registry.resolve(commandType)
            result = await handler.execute(session, command=command)
        except UnknownCommandTypeError as exc:
            return await self._handleFailure(
                session,
                commandId=commandId,
                instanceId=instanceId,
                attemptedCount=attemptedCount,
                message=str(exc),
                retryable=False,
                actor=workerId,
            )
        except CommandExecutionError as exc:
            return await self._handleFailure(
                session,
                commandId=commandId,
                instanceId=instanceId,
                attemptedCount=attemptedCount,
                message=exc.message,
                retryable=exc.retryable,
                actor=workerId,
            )
        except Exception as exc:  # noqa: BLE001 —— 失败隔离：兜住一切适配器异常
            logger.exception("命令执行异常（已收敛为失败）：commandId=%s type=%s", commandId, commandType)
            return await self._handleFailure(
                session,
                commandId=commandId,
                instanceId=instanceId,
                attemptedCount=attemptedCount,
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
                actor=workerId,
            )

        await self._store.completeCommand(session, commandId, STATUS_SUCCEEDED, result=result)
        await self._advanceAfterSuccess(session, instanceId=instanceId, stepId=stepId)
        await session.commit()

        return CommandOutcome(
            commandId=commandId,
            status=STATUS_SUCCEEDED,
            attempts=attemptedCount,
            result=result,
            isTerminal=True,
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
        """失败收敛：回滚半写副作用 → 显式落 FAILED + 次数 → 判定是否终态。"""

        await session.rollback()

        await self._store.failCommand(
            session,
            commandId,
            attempts=attemptedCount,
            errorMessage=message,
        )
        await session.commit()

        isTerminal = (not retryable) or attemptedCount >= self._maxAttempts
        if isTerminal and self._compensateOnFailure:
            await self.compensateInstance(session, instanceId, reason=message, actor=actor)

        return CommandOutcome(
            commandId=commandId,
            status=STATUS_FAILED,
            attempts=attemptedCount,
            errorMessage=message,
            isTerminal=isTerminal,
        )

    # ---------------------------------------------------------------- 推进

    async def _advanceAfterSuccess(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        stepId: int | None,
    ) -> None:
        """命令成功 ⇒ 步骤 SUCCEEDED；**所有**步骤都成功 ⇒ 实例 COMPLETED。

        没有这一步 ⇒ 步骤永远卡在 RUNNING、实例永远到不了 COMPLETED
        ⇒ 工作流的「完成」路径不可达（G6「状态机全路径」落空）。
        """

        if stepId is not None:
            await self._advanceStepToSucceeded(session, stepId)
        await self._completeInstanceIfStepsDone(session, instanceId)

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

    async def _completeInstanceIfStepsDone(
        self, session: AsyncSession, instanceId: int
    ) -> None:
        """全部步骤都成功（或跳过）⇒ 实例 COMPLETED。"""

        rows = (
            (
                await session.execute(
                    select(WorkflowStep).where(WorkflowStep.instance_id == instanceId)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        if not all(row.status in _STEP_DONE_STATUSES for row in rows):
            return

        instance = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if instance is None:
            return
        if instance.status in _INSTANCE_COMPLETABLE_STATUSES:
            await self._store.advanceInstance(session, instanceId, STATUS_COMPLETED)

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

        补偿是**尽力而为**：某条补偿失败不影响其余补偿（失败隔离），
        但会在结果里留痕（status=FAILED + 错误信息）。
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

        outcomes: list[CommandOutcome] = []
        compensatedStepIds: set[int] = set()

        for row in rows:
            commandId = row.id
            commandType = row.command_type
            stepId = row.step_id
            attempts = row.attempts

            handler = self._registry.resolveOptional(commandType)
            if handler is None or not isCompensable(handler):
                outcomes.append(
                    CommandOutcome(
                        commandId=commandId,
                        status=STATUS_SKIPPED,
                        attempts=attempts,
                        errorMessage="适配器不支持补偿（跳过，不假称已补偿）",
                    )
                )
                continue

            try:
                result = await handler.compensate(session, command=row)
            except Exception as exc:  # noqa: BLE001 —— 补偿失败隔离
                logger.exception("补偿失败：commandId=%s type=%s", commandId, commandType)
                await session.rollback()
                outcomes.append(
                    CommandOutcome(
                        commandId=commandId,
                        status=STATUS_FAILED,
                        attempts=attempts,
                        errorMessage=f"补偿失败：{type(exc).__name__}: {exc}",
                    )
                )
                continue

            await self._store.completeCommand(session, commandId, STATUS_COMPENSATED, result=result)
            if stepId is not None:
                compensatedStepIds.add(stepId)
            outcomes.append(
                CommandOutcome(
                    commandId=commandId,
                    status=STATUS_COMPENSATED,
                    attempts=attempts,
                    result=result,
                    isTerminal=True,
                )
            )

        for stepId in sorted(compensatedStepIds):
            await self._compensateStep(session, stepId)

        await self._compensateInstance(session, instanceId, reason=reason)
        await self._recordCompensation(session, instanceId=instanceId, reason=reason, actor=actor)
        await session.commit()

        return outcomes

    async def _recordCompensation(
        self, session: AsyncSession, *, instanceId: int, reason: str, actor: str
    ) -> None:
        """补偿留痕（治理审计：谁触发的补偿、为什么）。"""

        instance = await self._store.getInstance(session, instanceId)
        if instance is None:
            return
        await self._audit.recordCompensation(
            session,
            instance=instance,
            actor=actor,
            reason=reason,
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
