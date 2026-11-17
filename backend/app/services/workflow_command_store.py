"""P5 存储层 · **业务命令**（幂等入队 / 条件 UPDATE claim / 取消与失败）。

职责边界：**只**管 ``business_command`` 表。实例与步骤在
``workflow_instance_store.py``、审批在 ``workflow_approval_store.py``。

⚠️ **锁序**：命令 → 实例（与执行器「先领命令、后驱动实例」一致）⇒ 与取消路径
互锁会 40P01 死锁。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import BusinessCommand, WorkflowStep
from app.services.workflow_state_machine import assertWorkflowTransition
from app.services.workflow_store_base import (
    EVENT_COMMAND_ABORTED,
    EVENT_COMMAND_CANCELLED,
    EVENT_COMMAND_CLAIMED,
    EVENT_COMMAND_COMPLETED,
    EVENT_COMMAND_FAILED,
    EVENT_COMMAND_REQUESTED,
    MAX_EVENT_ERROR_LEN,
    MSG_STEP_INSTANCE_MISMATCH,
    STATUS_ABORTED,
    STATUS_CANCELLED,
    STATUS_EXECUTING,
    STATUS_FAILED,
    STATUS_PENDING,
    WorkflowStoreBase,
    liveInstanceIds,
    utcnow,
)

# 可被 claim 的来源状态：首次执行（PENDING）或重试（FAILED）。
CLAIMABLE_STATUSES = (STATUS_PENDING, STATUS_FAILED)

# 可被**取消**的状态：未领取（PENDING）或可重试失败（FAILED）。
# ⚠️ FAILED 也要能取消：它**在可领取集合里** ⇒ 不取消它，取消之后 Worker
# 照样会领走它去写外部系统 ⇒ 「取消」失真。
CANCELLABLE_STATUSES = (STATUS_PENDING, STATUS_FAILED)

# 需要被锁住（与 Worker 的 claim 互斥）的命令状态：未领取 / 可重试失败 / 在飞。
LOCKABLE_COMMAND_STATUSES = (STATUS_PENDING, STATUS_FAILED, STATUS_EXECUTING)


class WorkflowCommandStore(WorkflowStoreBase):
    """业务命令的持久化操作。"""

    async def enqueueCommand(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        stepId: int | None,
        commandType: str,
        idempotencyKey: str,
        payload: dict[str, Any],
        executedBy: str | None = None,
    ) -> tuple[BusinessCommand, bool]:
        """幂等入队业务命令。返回 ``(命令, 是否本次新建)``。

        步骤必须属于同一实例 ⇒ 拒绝跨实例挂载（写错实例会污染审计链）。
        """

        await self._assertStepBelongsToInstance(
            session, stepId=stepId, instanceId=instanceId
        )

        insertedId = await self._insertCommand(
            session,
            instanceId=instanceId,
            stepId=stepId,
            commandType=commandType,
            idempotencyKey=idempotencyKey,
            payload=payload,
            executedBy=executedBy,
        )

        existing = await self._loadCommandByIdempotency(
            session, commandType=commandType, idempotencyKey=idempotencyKey
        )

        if insertedId is not None:
            await self._recordCommandRequestedEvent(
                session,
                instanceId=instanceId,
                command=existing,
                stepId=stepId,
            )

        return existing, insertedId is not None

    async def _assertStepBelongsToInstance(
        self, session: AsyncSession, *, stepId: int | None, instanceId: int
    ) -> None:
        """步骤必须属于同一实例（跨实例挂载 ⇒ 拒绝）。"""

        if stepId is None:
            return
        stepInstanceId = (
            await session.execute(select(WorkflowStep.instance_id).where(WorkflowStep.id == stepId))
        ).scalar_one_or_none()
        if stepInstanceId is None or stepInstanceId != instanceId:
            raise ConflictError(MSG_STEP_INSTANCE_MISMATCH.format(stepId=stepId, instanceId=instanceId))

    async def _insertCommand(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        stepId: int | None,
        commandType: str,
        idempotencyKey: str,
        payload: dict[str, Any],
        executedBy: str | None,
    ) -> int | None:
        """插入命令行（幂等键冲突则不插）⇒ 返回新行 id；冲突时返回 ``None``。"""

        return (
            await session.execute(
                pg_insert(BusinessCommand)
                .values(
                    instance_id=instanceId,
                    step_id=stepId,
                    command_type=commandType,
                    idempotency_key=idempotencyKey,
                    status=STATUS_PENDING,
                    payload=payload,
                    executed_by=executedBy,
                )
                .on_conflict_do_nothing(index_elements=["command_type", "idempotency_key"])
                .returning(BusinessCommand.id)
            )
        ).scalar_one_or_none()

    async def _loadCommandByIdempotency(
        self, session: AsyncSession, *, commandType: str, idempotencyKey: str
    ) -> BusinessCommand:
        """按（命令类型, 幂等键）读回命令行。"""

        return (
            await session.execute(
                select(BusinessCommand)
                .where(
                    BusinessCommand.command_type == commandType,
                    BusinessCommand.idempotency_key == idempotencyKey,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _recordCommandRequestedEvent(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        command: BusinessCommand,
        stepId: int | None,
    ) -> None:
        """落「命令已入队」事件（只在本轮**新建**时写）。"""

        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_COMMAND_REQUESTED,
            payload={
                "commandId": command.id,
                "commandType": command.command_type,
                "stepId": stepId,
            },
        )

    async def claimCommand(
        self, session: AsyncSession, commandId: int, *, workerId: str, maxAttempts: int = 1
    ) -> BusinessCommand | None:
        """条件 UPDATE claim：``PENDING → EXECUTING``（重试时 ``FAILED → EXECUTING``）。

        ``maxAttempts`` = 该命令允许的**最大尝试次数**（毒丸防护）：已尝试次数
        达到上限 ⇒ 不再可领取（返回 ``None``）。
        ``maxAttempts=1``（默认）⇒ 只有 ``PENDING`` 可领（不重试），
        与 W1 的既有语义一致。

        **归属实例必须还活着**（``LIVE_INSTANCE_STATUSES``）：已取消 / 已补偿 /
        已完成的实例**不得**再发起外部写 ⇒ 它的命令一律领不走。
        （这一条是「取消 / 补偿 ⇒ 不再写外部系统」的**兜底**：即便取消路径漏了
        某条命令，Worker 也领不走它。）

        已被别人领走 / 已终态 / 次数耗尽 / 实例已停 ⇒ ``None``（不报错，不覆盖）。
        """

        row = (
            await session.execute(
                update(BusinessCommand)
                .where(
                    BusinessCommand.id == commandId,
                    BusinessCommand.status.in_(CLAIMABLE_STATUSES),
                    BusinessCommand.attempts < maxAttempts,
                    BusinessCommand.instance_id.in_(liveInstanceIds()),
                )
                .values(
                    status=STATUS_EXECUTING,
                    executed_by=workerId,
                    attempts=BusinessCommand.attempts + 1,
                    updated_time=utcnow(),
                )
                .returning(BusinessCommand)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is not None:
            await self.enqueueEvent(
                session,
                instanceId=row.instance_id,
                eventType=EVENT_COMMAND_CLAIMED,
                payload={"commandId": row.id, "workerId": workerId, "attempts": row.attempts},
            )
        return row

    async def claimNextCommand(
        self,
        session: AsyncSession,
        *,
        workerId: str,
        maxAttempts: int = 1,
        includeRetryable: bool = True,
    ) -> BusinessCommand | None:
        """领取一条待执行命令（``SKIP LOCKED``：跳过别人锁住的行）。

        候选 = ``PENDING``，或 ``FAILED`` 且尝试次数未达上限（重试候选），
        **且归属实例还活着**（已取消 / 已补偿 / 已完成的实例的命令不领）。

        ⚠️ ``includeRetryable=False`` ⇒ 只领 ``PENDING``（**不含**可重试失败）
        ⇒ Worker 的「排空」循环不会在**同一轮**里把刚失败的命令立刻再领一次
        （热转 ⇒ 重试没有节奏 ⇒ 毒丸命令把 worker 烧满）。
        """

        statuses = CLAIMABLE_STATUSES if includeRetryable else (STATUS_PENDING,)
        candidateId = (
            await session.execute(
                select(BusinessCommand.id)
                .where(
                    BusinessCommand.status.in_(statuses),
                    BusinessCommand.attempts < maxAttempts,
                    BusinessCommand.instance_id.in_(liveInstanceIds()),
                )
                .order_by(BusinessCommand.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if candidateId is None:
            return None
        return await self.claimCommand(session, candidateId, workerId=workerId, maxAttempts=maxAttempts)

    async def completeCommand(
        self,
        session: AsyncSession,
        commandId: int,
        toStatus: str,
        *,
        result: dict[str, Any] | None = None,
        errorMessage: str | None = None,
    ) -> BusinessCommand:
        """结束命令（行锁 + 状态机校验）。"""

        current = (
            await session.execute(
                select(BusinessCommand)
                .where(BusinessCommand.id == commandId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"业务命令 {commandId} 不存在")

        assertWorkflowTransition("command", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": utcnow()}
        if result is not None:
            values["result"] = result
        if errorMessage is not None:
            values["error_message"] = errorMessage

        updated = await self._writeStatus(
            session,
            BusinessCommand,
            commandId,
            fromStatus=current.status,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_COMMAND_COMPLETED,
            payload={"commandId": commandId, "to": toStatus},
        )
        return updated

    async def _lockCommand(self, session: AsyncSession, commandId: int) -> BusinessCommand:
        """在行锁内**重读**命令（`populate_existing` ⇒ 拿到最新状态）。"""

        current = (
            await session.execute(
                select(BusinessCommand)
                .where(BusinessCommand.id == commandId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"业务命令 {commandId} 不存在")
        return current

    async def cancelCommand(
        self,
        session: AsyncSession,
        commandId: int,
        *,
        reason: str,
    ) -> BusinessCommand:
        """把命令置 ``CANCELLED``（行锁 + 状态机校验）。

        可取消的来源状态：``PENDING``（未领取）与 ``FAILED``（可重试失败，
        **仍在可领取集合里** ⇒ 不取消它，Worker 取消之后照样领走）。

        ``EXECUTING`` 的取消只允许**执行器自己**发起（Worker 领走之后、
        真正调适配器之前发现实例已取消 ⇒ 不执行）——外部的取消路径遇到
        在飞的命令必须**失败关闭**（409），而不是假装取消成功。
        """

        current = await self._lockCommand(session, commandId)
        assertWorkflowTransition("command", current.status, STATUS_CANCELLED)

        updated = await self._writeStatus(
            session,
            BusinessCommand,
            commandId,
            fromStatus=current.status,
            values={
                "status": STATUS_CANCELLED,
                "error_message": reason,
                "updated_time": utcnow(),
            },
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_COMMAND_CANCELLED,
            payload={"commandId": commandId, "reason": reason[:MAX_EVENT_ERROR_LEN]},
        )
        return updated

    async def lockInstanceCommands(
        self, session: AsyncSession, *, instanceId: int
    ) -> list[BusinessCommand]:
        """锁住实例下所有**未终态**的命令（``PENDING`` / ``FAILED`` / ``EXECUTING``）。

        为什么必须锁：取消与 Worker 的 claim 必须**互斥**，否则
        「取消之后不再发起新的外部写」这条承诺会破：

        - 取消先拿到锁 ⇒ 命令被取消 ⇒ Worker 的 claim 条件 UPDATE 命中 0 行
          ⇒ Worker 跳过（外部写不发生）；
        - Worker 先拿到锁 ⇒ 取消等它提交 ⇒ 看到 ``EXECUTING`` ⇒ 取消**失败关闭**
          （409），调用方稍后重试。

        ⚠️ **锁序**：命令 → 实例（与执行器「先领命令、后驱动实例」一致），
        否则与取消路径互锁 ⇒ 40P01 死锁。
        """

        rows = (
            (
                await session.execute(
                    select(BusinessCommand)
                    .where(
                        BusinessCommand.instance_id == instanceId,
                        BusinessCommand.status.in_(LOCKABLE_COMMAND_STATUSES),
                    )
                    .order_by(BusinessCommand.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def cancelPendingCommands(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        reason: str,
    ) -> set[int]:
        """取消实例下所有**可取消**（``PENDING`` / ``FAILED``）的命令。

        返回被取消命令所挂的**步骤 id 集合**（无步骤的命令不计入）⇒ 调用方据此把
        对应步骤记为 SKIPPED（命令没跑 ⇒ 这一步也没跑）。

        为什么必须做：``CANCELLED`` 不在可领取状态集合里 ⇒ 取消后 Worker
        再也领不到它们 ⇒ 「取消」才是真的取消（否则外部系统照写）。
        """

        rows = (
            (
                await session.execute(
                    select(BusinessCommand)
                    .where(
                        BusinessCommand.instance_id == instanceId,
                        BusinessCommand.status.in_(CANCELLABLE_STATUSES),
                    )
                    .order_by(BusinessCommand.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        cancelledStepIds: set[int] = set()
        for row in rows:
            await self.cancelCommand(session, row.id, reason=reason)
            if row.step_id is not None:
                cancelledStepIds.add(row.step_id)
        return cancelledStepIds

    async def failCommand(
        self,
        session: AsyncSession,
        commandId: int,
        *,
        attempts: int,
        errorMessage: str,
        terminal: bool = False,
    ) -> BusinessCommand:
        """把命令置 ``FAILED`` 并**显式写回尝试次数**。

        为什么必须显式写 attempts：执行器在适配器抛错后用 **SAVEPOINT** 回滚
        「适配器写的那一段」（撤销适配器的半写副作用），而 claim 时的
        ``attempts + 1`` 留在外层事务里 ⇒ 次数**不会**丢；但这里仍**显式**写回，
        让「落库值」不依赖调用方的事务边界（幂等、可读）。

        ``terminal=True`` ⇒ 落 ``ABORTED``（**终态失败**：业务上不可重试 /
        尝试次数耗尽）⇒ 落库即终态 ⇒ Worker 不会再把它当「可重试失败」重领。
        （终态此前只存在于内存 ⇒ 库里仍是 FAILED 且次数未耗尽 ⇒ 无限重试。）

        ⚠️ 来源状态可以是 ``EXECUTING``（失败收敛保留 claim ⇒ 直接落终态）。
        """

        current = await self._lockCommand(session, commandId)

        toStatus = STATUS_ABORTED if terminal else STATUS_FAILED
        assertWorkflowTransition("command", current.status, toStatus)

        updated = await self._writeStatus(
            session,
            BusinessCommand,
            commandId,
            fromStatus=current.status,
            values={
                "status": toStatus,
                # ⚠️ **只增不减**（``GREATEST``）：并发失败时两次写回可能带着
                #   **不同的**尝试次数（谁后提交谁生效）⇒ 若直接覆盖，次数会
                #   **往回退** ⇒ 毒丸命令永远达不到上限 ⇒ 无限重试。
                "attempts": func.greatest(BusinessCommand.attempts, attempts),
                "error_message": errorMessage,
                "updated_time": utcnow(),
            },
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_COMMAND_ABORTED if terminal else EVENT_COMMAND_FAILED,
            payload={
                "commandId": commandId,
                "attempts": attempts,
                "error": errorMessage[:MAX_EVENT_ERROR_LEN],
            },
        )
        return updated
