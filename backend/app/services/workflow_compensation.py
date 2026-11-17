"""P5 **补偿器**（把已成功的外部写回滚掉，并把实例 / 步骤收敛到终态）。

为什么独立成模块：补偿是**一条独立的事务线**（命令 → 实例 → 步骤），
与「执行一条命令」的职责不同 ⇒ 混在执行器里会让那个文件超长、也让两条线
的锁序混在一起看不清。

三条纪律：

1. **待执行命令一并取消**：否则「回滚」只是纸面动作 —— Worker 照样领走
   ``PENDING`` 命令去写外部系统。
2. **步骤一律收敛**：实例 ``COMPENSATED`` ⇒ 不得还有步骤停在 ``PENDING`` /
   ``RUNNING``（步骤矩阵里这两个状态都有出边 = 这一轮还没结束）。命令**没跑
   成功**的步骤记 ``SKIPPED``（含①**从没入队**的步骤 ②命令**被取消**的步骤
   ③命令**终态失败**（``ABORTED``）挂着的步骤）；命令**跑过并被回滚**的步骤
   记 ``COMPENSATED``。
3. **失败隔离用 SAVEPOINT**：整事务 ``rollback()`` 会把「已取消的待执行命令」
   和「已完成的补偿」一起撤销 ⇒ 每条补偿一个 SAVEPOINT，失败只回滚那一条。

⚠️ **锁序**：命令 → 实例 → 步骤（与取消路径「命令 → 实例 → 任务」、
决策路径「实例 → 任务」同一条线 ⇒ 不会 40P01）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.workflow_models import BusinessCommand, WorkflowInstance, WorkflowStep
from app.services.workflow_audit_service import WorkflowAuditService
from app.services.workflow_command_ports import CommandHandlerRegistry, isCompensable
from app.services.workflow_command_types import (
    STATUS_COMPENSATED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCEEDED,
    CommandOutcome,
)
from app.services.workflow_errors import sanitizeErrorMessage
from app.services.workflow_store import WorkflowStore

logger = logging.getLogger(__name__)

# 补偿是**系统事件**（执行失败触发的自动回滚）⇒ 默认 actor 用系统标识。
SYSTEM_ACTOR_COMPENSATION = "system:command-compensation"


@dataclass(frozen=True)
class CompensationSummary:
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


class CompensationRunner:
    """补偿器（逆序回滚同实例内**已成功**的命令）。"""

    def __init__(
        self,
        registry: CommandHandlerRegistry,
        *,
        store: WorkflowStore | None = None,
        audit: WorkflowAuditService | None = None,
    ) -> None:
        self._registry = registry
        self._store = store or WorkflowStore()
        self._audit = audit or WorkflowAuditService()

    async def compensateInstance(
        self,
        session: AsyncSession,
        instanceId: int,
        *,
        reason: str,
        actor: str = SYSTEM_ACTOR_COMPENSATION,
    ) -> list[CommandOutcome]:
        """补偿同实例内**已成功**的命令（逆序），并把步骤 / 实例收敛到 COMPENSATED。

        补偿是**尽力而为**：某条补偿失败**不影响**其余补偿（失败隔离），但会在
        结果里留痕（status=FAILED + 错误信息）。
        """

        await self._store.cancelPendingCommands(
            session,
            instanceId=instanceId,
            reason=f"实例已补偿，待执行命令不再执行：{reason}",
        )

        targets = await self._loadCompensationTargets(session, instanceId)
        summary = await self._compensateAll(session, targets)

        # 收敛实例（命令 → 实例）；**返回补偿前的真实状态**（审计的 before）
        priorStatus = await self._compensateInstance(session, instanceId, reason=reason)
        # 收敛步骤（命令 → 实例 → 步骤）
        for stepId in sorted(summary.compensatedStepIds):
            await self._compensateStep(session, stepId)
        # 其余步骤（命令没跑成功）⇒ SKIPPED（已补偿的步骤优先：它跑过 ⇒ COMPENSATED）
        await self._skipRemainingSteps(
            session, instanceId=instanceId, keep=summary.compensatedStepIds
        )

        # 留痕：**只在确实尝试过补偿时**写（不假称「已补偿」），并带上计数。
        if summary.total > 0:
            await self._recordCompensation(
                session,
                instanceId=instanceId,
                reason=reason,
                actor=actor,
                priorStatus=priorStatus,
                succeededCount=summary.succeeded,
                failedCount=summary.failed,
                skippedCount=summary.skipped,
            )
        await session.commit()

        return list(summary.outcomes)

    async def _compensateAll(
        self, session: AsyncSession, targets: Sequence[tuple[int, str, int | None, int]]
    ) -> CompensationSummary:
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

        return CompensationSummary(
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
        """补偿**一条**命令（失败隔离：只回滚这一条 ⇒ SAVEPOINT）。"""

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
                errorMessage=f"补偿失败：{type(exc).__name__}: {sanitizeErrorMessage(str(exc))}",
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
        priorStatus: str | None,
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
            beforeStatus=priorStatus,
            succeededCount=succeededCount,
            failedCount=failedCount,
            skippedCount=skippedCount,
        )

    async def _skipRemainingSteps(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        keep: frozenset[int] | set[int],
    ) -> None:
        """实例里**其余**步骤 → SKIPPED（命令没跑成功 ⇒ 这一步也没跑）。

        ⚠️ **必须扫全实例的步骤**，不能只看「被取消的命令所挂的步骤」：
        ①**从没入队**的步骤（``PENDING``）压根没有命令行 ⇒ 不在任何命令集合里；
        ②命令**终态失败**（``ABORTED``）挂着的步骤是 ``RUNNING`` ⇒ 既没被取消
        （``ABORTED`` 不在可取消集合里）也没被补偿 ⇒ 之前一直吊着。
        ⇒ 两条都靠这里收敛 ⇒ **实例 COMPENSATED 时没有步骤停在 PENDING/RUNNING**
        （步骤矩阵里这两个状态都有出边 = 这一轮还没结束）。

        ⚠️ **在飞命令的步骤不动**：命令还在 ``EXECUTING``（别的 worker 正在跑）
        ⇒ 它的步骤由那条命令自己收敛（成功 → SUCCEEDED，失败 → 补偿）⇒ 这里
        抢先写 SKIPPED 会让它的状态推进撞上非法转移。
        """

        inFlightStepIds = await self._executingStepIds(session, instanceId=instanceId)
        for step in await self._store.listSteps(session, instanceId=instanceId):
            if step.id in keep or step.id in inFlightStepIds:
                continue
            if step.status in {STATUS_PENDING, STATUS_RUNNING}:
                await self._store.advanceStep(session, step.id, STATUS_SKIPPED)

    async def _executingStepIds(self, session: AsyncSession, *, instanceId: int) -> set[int]:
        """本实例**在飞**（``EXECUTING``）命令所挂的步骤 id 集合。"""

        rows = (
            (
                await session.execute(
                    select(BusinessCommand.step_id)
                    .where(
                        BusinessCommand.instance_id == instanceId,
                        BusinessCommand.status == "EXECUTING",
                    )
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return {rowId for rowId in rows if rowId is not None}

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
        if status == STATUS_RUNNING:
            await self._store.advanceStep(session, stepId, STATUS_FAILED)
            await self._store.advanceStep(session, stepId, STATUS_COMPENSATED)
            return
        if status == STATUS_FAILED:
            await self._store.advanceStep(session, stepId, STATUS_COMPENSATED)

    async def _compensateInstance(
        self, session: AsyncSession, instanceId: int, *, reason: str
    ) -> str | None:
        """实例 → FAILED → COMPENSATED（状态机只允许 COMPENSATED 来自 FAILED / COMPLETED）。

        返回**补偿前的真实状态**（审计的 before 快照用）——不许写死 ``FAILED``：
        补偿往往发生在实例还 ``RUNNING`` 时 ⇒ 写死就是**假审计**。
        """

        current = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            return None

        priorStatus = current.status
        status = priorStatus
        if status in {"PENDING", STATUS_RUNNING, "WAITING_APPROVAL", "APPROVED"}:
            await self._store.advanceInstance(session, instanceId, STATUS_FAILED, errorMessage=reason)
            status = STATUS_FAILED
        if status in {STATUS_FAILED, STATUS_COMPLETED}:
            await self._store.advanceInstance(session, instanceId, STATUS_COMPENSATED)
        return priorStatus
