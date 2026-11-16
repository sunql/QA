"""P5 工作流存储层：幂等创建 / 行锁推进 / 条件 UPDATE claim。

三条纪律（对应实施计划 D-2 / D-3 / D-4）：

1. **幂等**（D-2）：创建实例 / 入队命令都用
   ``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` —— 一条语句原子完成
   「不存在才插入」⇒ 并发下重复提交也只落一行；命中冲突时返回既有行并
   ``created=False``（**幂等重放不是错误**）。
2. **行锁 + 重读**（D-3）：推进前在锁内 ``SELECT ... FOR UPDATE`` 且带
   ``execution_options(populate_existing=True)``。**`populate_existing` 不可省**：
   调用方通常早已把这一行进 identity map，SQLAlchemy 默认不刷新既有实例属性
   ⇒ 拿到锁却按陈旧值判定流转 ⇒ 并发下**丢步**（两个推进都落地，实例跳过中间态）。
3. **条件 UPDATE claim**（D-3）：``WHERE id=? AND status='PENDING'`` ⇒ 同一命令
   只能被 claim 一次；候选选取用 ``SKIP LOCKED`` ⇒ 多 worker 各拿各的。

状态流转一律先过**纯函数矩阵**（`app/services/workflow_state_machine.py`）⇒
非法流转在写库前就被拒（拒绝即零副作用）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowInstance,
    WorkflowOutbox,
    WorkflowStep,
)
from app.services.workflow_state_machine import assertWorkflowTransition

# 命令 / 步骤的初始状态字面量（与状态机矩阵、库层 CHECK 一致）。
_STATUS_PENDING = "PENDING"
_STATUS_EXECUTING = "EXECUTING"
_STATUS_FAILED = "FAILED"

# 可被 claim 的来源状态：首次执行（PENDING）或重试（FAILED）。
_CLAIMABLE_STATUSES = (_STATUS_PENDING, _STATUS_FAILED)

# 审批任务的初始状态（与状态机矩阵、库层 CHECK 一致）。
_APPROVAL_PENDING = "PENDING"

# 领域事件类型（**每步可追溯**的事件流词汇表；worker 据此路由）。
#
# ⚠️ 事件行与业务写**同事务**（只 add 不 commit）⇒ 业务回滚时事件一起消失，
# 不会留下「幽灵事件」。
EVENT_INSTANCE_CREATED = "instance_created"
EVENT_STEPS_CREATED = "steps_created"
EVENT_INSTANCE_STATUS_CHANGED = "instance_status_changed"
EVENT_STEP_STATUS_CHANGED = "step_status_changed"
EVENT_COMMAND_REQUESTED = "command_requested"
EVENT_COMMAND_CLAIMED = "command_claimed"
EVENT_COMMAND_COMPLETED = "command_completed"
EVENT_COMMAND_FAILED = "command_failed"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_DECIDED = "approval_decided"
EVENT_APPROVAL_TIMED_OUT = "approval_timed_out"

# 事件载荷里的错误摘要长度上限（错误全文已落 business_command.error_message）。
_MAX_EVENT_ERROR_LEN = 500

# 幂等冲突的错误信息（ConflictError ⇒ 409）。
_MSG_STEP_INSTANCE_MISMATCH = "步骤 {stepId} 不属于实例 {instanceId}（拒绝跨实例挂载）"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowStore:
    """工作流实例 / 步骤 / 命令的持久化操作。

    **不 commit**：所有写操作与调用方业务写在同一事务里原子提交（照
    `app/services/outbox_service.py` 的同事务语义）。

    **同时是「每步可追溯」的唯一入口**：所有状态流转都在这里写一条
    ``workflow_outbox`` 事件（同事务）⇒ 事件流不可能被绕过。
    """

    # ---------------------------------------------------------------- 事件流

    async def enqueueEvent(
        self,
        session: AsyncSession,
        *,
        instanceId: int | None,
        eventType: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowOutbox:
        """把领域事件写入 P5 自有 outbox（**同事务**：只 add 不 commit）。

        「每步可追溯」的事实流：实例创建 / 步骤创建 / 状态流转 / 命令请求 /
        审批请求 / 审批决策 / 超时 全部走这里。
        """

        row = WorkflowOutbox(
            instance_id=instanceId,
            event_type=eventType,
            payload=payload or {},
        )
        session.add(row)
        return row

    # ---------------------------------------------------------------- 实例

    async def getInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance | None:
        """无锁读实例（返回 None 表示不存在）。"""

        return (
            await session.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
        ).scalar_one_or_none()

    async def createInstance(
        self,
        session: AsyncSession,
        *,
        definitionId: int,
        idempotencyKey: str,
        requestJson: dict[str, Any],
        actor: str,
        businessKey: str | None = None,
    ) -> tuple[WorkflowInstance, bool]:
        """幂等创建实例。返回 ``(实例, 是否本次新建)``。

        重复提交（同一 ``definitionId`` + ``idempotencyKey``）⇒ 返回既有实例，
        ``created=False``，**不抛错**（幂等重放是正常路径）。
        """

        insertedId = (
            await session.execute(
                pg_insert(WorkflowInstance)
                .values(
                    definition_id=definitionId,
                    idempotency_key=idempotencyKey,
                    business_key=businessKey,
                    status=_STATUS_PENDING,
                    request_json=requestJson,
                    created_by=actor,
                )
                .on_conflict_do_nothing(index_elements=["definition_id", "idempotency_key"])
                .returning(WorkflowInstance.id)
            )
        ).scalar_one_or_none()

        existing = (
            await session.execute(
                select(WorkflowInstance)
                .where(
                    WorkflowInstance.definition_id == definitionId,
                    WorkflowInstance.idempotency_key == idempotencyKey,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        if insertedId is not None:
            # 只在**本次新建**时留事件（幂等重放不重复留痕）
            await self.enqueueEvent(
                session,
                instanceId=existing.id,
                eventType=EVENT_INSTANCE_CREATED,
                payload={
                    "definitionId": definitionId,
                    "idempotencyKey": idempotencyKey,
                    "actor": actor,
                },
            )

        return existing, insertedId is not None

    async def advanceInstance(
        self,
        session: AsyncSession,
        instanceId: int,
        toStatus: str,
        *,
        currentStep: str | None = None,
        errorMessage: str | None = None,
        resultJson: dict[str, Any] | None = None,
    ) -> WorkflowInstance:
        """推进实例状态（行锁 + 状态机校验）。"""

        current = await self._lockInstance(session, instanceId)
        assertWorkflowTransition("instance", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
        if currentStep is not None:
            values["current_step"] = currentStep
        if errorMessage is not None:
            values["error_message"] = errorMessage
        if resultJson is not None:
            values["result_json"] = resultJson

        updated = await self._writeStatus(
            session,
            WorkflowInstance,
            instanceId,
            fromStatus=current.status,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_INSTANCE_STATUS_CHANGED,
            payload={"from": current.status, "to": toStatus},
        )
        return updated

    async def _lockInstance(self, session: AsyncSession, instanceId: int) -> WorkflowInstance:
        """在行锁内**重读**实例（`populate_existing` ⇒ 拿到最新状态）。"""

        current = (
            await session.execute(
                select(WorkflowInstance)
                .where(WorkflowInstance.id == instanceId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"工作流实例 {instanceId} 不存在")
        return current

    # ---------------------------------------------------------------- 步骤

    async def createSteps(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        steps: Sequence[dict[str, Any]],
    ) -> list[WorkflowStep]:
        """按编排创建实例的步骤（初始状态 PENDING）。

        同一实例内步骤编码必须唯一 ⇒ 重复直接拒（ConflictError），
        **不靠库层唯一约束报错**（那会变成 500 而不是可读的 409）。
        库层唯一约束仍是并发下的兜底。
        """

        existingCodes = set(
            (
                await session.execute(
                    select(WorkflowStep.step_code).where(
                        WorkflowStep.instance_id == instanceId
                    )
                )
            )
            .scalars()
            .all()
        )
        codes = [str(step["code"]) for step in steps]
        seen: set[str] = set()
        duplicates: set[str] = set()
        for code in codes:
            if code in existingCodes or code in seen:
                duplicates.add(code)
            seen.add(code)
        if duplicates:
            raise ConflictError(
                f"步骤编码重复：{', '.join(sorted(duplicates))}（实例内必须唯一）"
            )

        rows = [
            WorkflowStep(
                instance_id=instanceId,
                step_code=step["code"],
                step_order=int(step.get("order", index)),
                status=_STATUS_PENDING,
                input_json=step.get("input"),
            )
            for index, step in enumerate(steps)
        ]
        session.add_all(rows)
        await session.flush()
        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_STEPS_CREATED,
            payload={"stepCodes": [row.step_code for row in rows]},
        )
        return rows

    async def advanceStep(
        self,
        session: AsyncSession,
        stepId: int,
        toStatus: str,
        *,
        outputJson: dict[str, Any] | None = None,
        errorMessage: str | None = None,
    ) -> WorkflowStep:
        """推进步骤状态（行锁 + 状态机校验）。"""

        current = (
            await session.execute(
                select(WorkflowStep)
                .where(WorkflowStep.id == stepId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"工作流步骤 {stepId} 不存在")

        assertWorkflowTransition("step", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
        if outputJson is not None:
            values["output_json"] = outputJson
        if errorMessage is not None:
            values["error_message"] = errorMessage
        if toStatus == "RUNNING" and current.started_at is None:
            values["started_at"] = _utcnow()
        if toStatus in {"SUCCEEDED", "FAILED", "SKIPPED", "COMPENSATED"}:
            values["finished_at"] = _utcnow()

        updated = await self._writeStatus(
            session,
            WorkflowStep,
            stepId,
            fromStatus=current.status,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_STEP_STATUS_CHANGED,
            payload={"stepId": stepId, "from": current.status, "to": toStatus},
        )
        return updated

    # ---------------------------------------------------------------- 命令

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

        if stepId is not None:
            stepInstanceId = (
                await session.execute(select(WorkflowStep.instance_id).where(WorkflowStep.id == stepId))
            ).scalar_one_or_none()
            if stepInstanceId is None or stepInstanceId != instanceId:
                raise ConflictError(_MSG_STEP_INSTANCE_MISMATCH.format(stepId=stepId, instanceId=instanceId))

        insertedId = (
            await session.execute(
                pg_insert(BusinessCommand)
                .values(
                    instance_id=instanceId,
                    step_id=stepId,
                    command_type=commandType,
                    idempotency_key=idempotencyKey,
                    status=_STATUS_PENDING,
                    payload=payload,
                    executed_by=executedBy,
                )
                .on_conflict_do_nothing(index_elements=["command_type", "idempotency_key"])
                .returning(BusinessCommand.id)
            )
        ).scalar_one_or_none()

        existing = (
            await session.execute(
                select(BusinessCommand)
                .where(
                    BusinessCommand.command_type == commandType,
                    BusinessCommand.idempotency_key == idempotencyKey,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        if insertedId is not None:
            await self.enqueueEvent(
                session,
                instanceId=instanceId,
                eventType=EVENT_COMMAND_REQUESTED,
                payload={
                    "commandId": existing.id,
                    "commandType": commandType,
                    "stepId": stepId,
                },
            )

        return existing, insertedId is not None

    async def claimCommand(
        self, session: AsyncSession, commandId: int, *, workerId: str, maxAttempts: int = 1
    ) -> BusinessCommand | None:
        """条件 UPDATE claim：``PENDING → EXECUTING``（重试时 ``FAILED → EXECUTING``）。

        ``maxAttempts`` = 该命令允许的**最大尝试次数**（毒丸防护）：已尝试次数
        达到上限 ⇒ 不再可领取（返回 ``None``）。
        ``maxAttempts=1``（默认）⇒ 只有 ``PENDING`` 可领（不重试），
        与 W1 的既有语义一致。

        已被别人领走 / 已终态 / 次数耗尽 ⇒ ``None``（不报错，不覆盖）。
        """

        row = (
            await session.execute(
                update(BusinessCommand)
                .where(
                    BusinessCommand.id == commandId,
                    BusinessCommand.status.in_(_CLAIMABLE_STATUSES),
                    BusinessCommand.attempts < maxAttempts,
                )
                .values(
                    status=_STATUS_EXECUTING,
                    executed_by=workerId,
                    attempts=BusinessCommand.attempts + 1,
                    updated_time=_utcnow(),
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
        self, session: AsyncSession, *, workerId: str, maxAttempts: int = 1
    ) -> BusinessCommand | None:
        """领取一条待执行命令（``SKIP LOCKED``：跳过别人锁住的行）。

        候选 = ``PENDING``，或 ``FAILED`` 且尝试次数未达上限（重试候选）。
        """

        candidateId = (
            await session.execute(
                select(BusinessCommand.id)
                .where(
                    BusinessCommand.status.in_(_CLAIMABLE_STATUSES),
                    BusinessCommand.attempts < maxAttempts,
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

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
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

    async def failCommand(
        self,
        session: AsyncSession,
        commandId: int,
        *,
        attempts: int,
        errorMessage: str,
    ) -> BusinessCommand:
        """把命令置 ``FAILED`` 并**显式写回尝试次数**。

        为什么必须显式写 attempts：执行器在适配器抛错后会 ``rollback()`` 回滚
        「本次执行」的事务（撤销适配器的半写副作用），而 claim 时的
        ``attempts + 1`` 与它在同一事务里 ⇒ 回滚后次数丢失 ⇒ 毒丸命令会被
        无限重试。故失败路径必须**显式**把尝试次数落库。
        """

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

        assertWorkflowTransition("command", current.status, _STATUS_FAILED)

        updated = await self._writeStatus(
            session,
            BusinessCommand,
            commandId,
            fromStatus=current.status,
            values={
                "status": _STATUS_FAILED,
                "attempts": attempts,
                "error_message": errorMessage,
                "updated_time": _utcnow(),
            },
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_COMMAND_FAILED,
            payload={
                "commandId": commandId,
                "attempts": attempts,
                "error": errorMessage[:_MAX_EVENT_ERROR_LEN],
            },
        )
        return updated

    # ---------------------------------------------------------------- 审批

    async def getApprovalTask(self, session: AsyncSession, taskId: int) -> ApprovalTask | None:
        """无锁读审批任务（返回 None 表示不存在）。"""

        return (
            await session.execute(
                select(ApprovalTask).where(ApprovalTask.id == taskId)
            )
        ).scalar_one_or_none()

    async def listApprovalTasks(self, session: AsyncSession, *, instanceId: int) -> list[ApprovalTask]:
        """列出实例的审批任务（按 id 升序）。"""

        rows = (
            (
                await session.execute(
                    select(ApprovalTask)
                    .where(ApprovalTask.instance_id == instanceId)
                    .order_by(ApprovalTask.id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def createApprovalTask(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        subjectType: str,
        subjectId: str,
        matrixCode: str,
        approverRole: str | None,
        approverUser: str | None,
        dueAt: datetime,
        createdBy: str | None,
    ) -> tuple[ApprovalTask, bool]:
        """幂等创建审批任务。返回 ``(任务, 是否本次新建)``。

        幂等键 = ``(instance_id, matrix_code)``（库层唯一约束 ``uq_approval_task_matrix``）
        ⇒ 同一实例同一矩阵条目只落一条任务。
        """

        insertedId = (
            await session.execute(
                pg_insert(ApprovalTask)
                .values(
                    instance_id=instanceId,
                    subject_type=subjectType,
                    subject_id=subjectId,
                    matrix_code=matrixCode,
                    status=_APPROVAL_PENDING,
                    approver_role=approverRole,
                    approver_user=approverUser,
                    due_at=dueAt,
                    created_by=createdBy,
                )
                .on_conflict_do_nothing(index_elements=["instance_id", "matrix_code"])
                .returning(ApprovalTask.id)
            )
        ).scalar_one_or_none()

        existing = (
            await session.execute(
                select(ApprovalTask)
                .where(
                    ApprovalTask.instance_id == instanceId,
                    ApprovalTask.matrix_code == matrixCode,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        if insertedId is not None:
            await self.enqueueEvent(
                session,
                instanceId=instanceId,
                eventType=EVENT_APPROVAL_REQUESTED,
                payload={
                    "taskId": existing.id,
                    "matrixCode": matrixCode,
                    "subjectType": subjectType,
                    "subjectId": subjectId,
                    "dueAt": dueAt.isoformat() if dueAt is not None else None,
                },
            )

        return existing, insertedId is not None

    async def decideApprovalTask(
        self,
        session: AsyncSession,
        taskId: int,
        *,
        fromStatus: str,
        toStatus: str,
        actor: str,
        note: str | None = None,
        decidedAt: datetime | None = None,
    ) -> ApprovalTask:
        """决策审批任务（行锁 + 状态机校验 + 条件 UPDATE）。

        ``approver_user`` 在此刻写入 = **实际审批人（签署人）**；
        「谁有资格批」由定义体的审批矩阵判定，不落在任务行上。
        """

        current = await self._lockApprovalTask(session, taskId)
        assertWorkflowTransition("approval", current.status, toStatus)

        values: dict[str, Any] = {
            "status": toStatus,
            "updated_time": _utcnow(),
            "approver_user": actor,
        }
        if decidedAt is not None:
            values["decided_at"] = decidedAt
        if note is not None:
            values["decision_note"] = note

        updated = await self._writeStatus(
            session,
            ApprovalTask,
            taskId,
            fromStatus=fromStatus,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=EVENT_APPROVAL_DECIDED,
            payload={
                "taskId": taskId,
                "from": fromStatus,
                "to": toStatus,
                "actor": actor,
            },
        )
        return updated

    async def markApprovalTask(
        self,
        session: AsyncSession,
        taskId: int,
        *,
        fromStatus: str,
        toStatus: str,
        decidedAt: datetime | None = None,
    ) -> ApprovalTask:
        """推进审批任务状态（不写签署人）—— 超时置 TIMEOUT 走这条。"""

        current = await self._lockApprovalTask(session, taskId)
        assertWorkflowTransition("approval", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": _utcnow()}
        if decidedAt is not None:
            values["decided_at"] = decidedAt

        updated = await self._writeStatus(
            session,
            ApprovalTask,
            taskId,
            fromStatus=fromStatus,
            values=values,
        )
        await self.enqueueEvent(
            session,
            instanceId=current.instance_id,
            eventType=(
                EVENT_APPROVAL_TIMED_OUT if toStatus == "TIMEOUT" else EVENT_APPROVAL_DECIDED
            ),
            payload={"taskId": taskId, "from": fromStatus, "to": toStatus},
        )
        return updated

    async def _lockApprovalTask(self, session: AsyncSession, taskId: int) -> ApprovalTask:
        """在行锁内**重读**审批任务（`populate_existing` ⇒ 拿到最新状态）。"""

        current = (
            await session.execute(
                select(ApprovalTask)
                .where(ApprovalTask.id == taskId)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise NotFoundError(f"审批任务 {taskId} 不存在")
        return current

    # ---------------------------------------------------------------- 内部

    @staticmethod
    async def _writeStatus(
        session: AsyncSession,
        model: type,
        rowId: int,
        *,
        fromStatus: str,
        values: dict[str, Any],
    ) -> Any:
        """按「当前状态」条件 UPDATE（双保险：行锁之外再加一道状态判定）。

        条件 UPDATE 命中 0 行 ⇒ 状态已被并发改动 ⇒ 拒绝（不静默覆盖）。
        """

        row = (
            await session.execute(
                update(model)
                .where(model.id == rowId, model.status == fromStatus)
                .values(**values)
                .returning(model)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ConflictError(f"状态已被并发改动，拒绝覆盖（期望当前状态 {fromStatus}）")
        return row
