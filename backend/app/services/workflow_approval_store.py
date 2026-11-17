"""P5 存储层 · **审批任务**（幂等创建 / 签署决策 / 超时与取消）。

职责边界：**只**管 ``approval_task`` 表。实例与步骤在
``workflow_instance_store.py``、命令在 ``workflow_command_store.py``。

⚠️ ``approver_user`` 在**决策那一刻**写入 = 实际签署人；「谁有资格批」由定义体的
审批矩阵判定，**不**落在任务行上。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.workflow_models import (
    ApprovalTask,
    WorkflowDefinition,
    WorkflowInstance,
)
from app.services.workflow_state_machine import assertWorkflowTransition
from app.services.workflow_store_base import (
    EVENT_APPROVAL_DECIDED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_TIMED_OUT,
    WorkflowStoreBase,
    utcnow,
)

# 审批任务的初始状态（与状态机矩阵、库层 CHECK 一致）。
APPROVAL_PENDING = "PENDING"


class WorkflowApprovalStore(WorkflowStoreBase):
    """审批任务的持久化操作。"""

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

    async def listTasksWithContext(
        self,
        session: AsyncSession,
        *,
        statuses: Sequence[str] | None,
        limit: int,
    ) -> list[tuple[ApprovalTask, WorkflowInstance, WorkflowDefinition]]:
        """列出审批任务（带实例与定义上下文），按到期时刻升序。

        ⚠️ **不带授权过滤**：审批持有人匹配需要定义体审批矩阵
        （``approver_user`` 列只在决策那一刻写签署人 ⇒ 待审批时为空），
        SQL 无法只看任务行判定 ⇒ 由服务层用 ``_matchesHolder`` 过滤。
        调用方必须给 ``limit`` 上界（扫描上限），不能拉全表。
        """

        stmt = (
            select(ApprovalTask, WorkflowInstance, WorkflowDefinition)
            .join(WorkflowInstance, WorkflowInstance.id == ApprovalTask.instance_id)
            .join(
                WorkflowDefinition,
                WorkflowDefinition.id == WorkflowInstance.definition_id,
            )
            .order_by(ApprovalTask.due_at.asc(), ApprovalTask.id.asc())
            .limit(limit)
        )
        if statuses is not None:
            stmt = stmt.where(ApprovalTask.status.in_(statuses))
        rows = (await session.execute(stmt)).all()
        return [(task, instance, definition) for task, instance, definition in rows]

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

        insertedId = await self._insertApprovalTask(
            session,
            instanceId=instanceId,
            subjectType=subjectType,
            subjectId=subjectId,
            matrixCode=matrixCode,
            approverRole=approverRole,
            approverUser=approverUser,
            dueAt=dueAt,
            createdBy=createdBy,
        )

        existing = await self._loadApprovalTaskByMatrix(
            session, instanceId=instanceId, matrixCode=matrixCode
        )

        if insertedId is not None:
            await self._recordApprovalRequestedEvent(
                session,
                instanceId=instanceId,
                task=existing,
                subjectType=subjectType,
                subjectId=subjectId,
                dueAt=dueAt,
            )

        return existing, insertedId is not None

    async def _insertApprovalTask(
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
    ) -> int | None:
        """插入审批任务行（冲突则不插）⇒ 返回新行 id；冲突时返回 ``None``。"""

        return (
            await session.execute(
                pg_insert(ApprovalTask)
                .values(
                    instance_id=instanceId,
                    subject_type=subjectType,
                    subject_id=subjectId,
                    matrix_code=matrixCode,
                    status=APPROVAL_PENDING,
                    approver_role=approverRole,
                    approver_user=approverUser,
                    due_at=dueAt,
                    created_by=createdBy,
                )
                .on_conflict_do_nothing(index_elements=["instance_id", "matrix_code"])
                .returning(ApprovalTask.id)
            )
        ).scalar_one_or_none()

    async def _loadApprovalTaskByMatrix(
        self, session: AsyncSession, *, instanceId: int, matrixCode: str
    ) -> ApprovalTask:
        """按（实例, 矩阵条目）读回审批任务行。"""

        return (
            await session.execute(
                select(ApprovalTask)
                .where(
                    ApprovalTask.instance_id == instanceId,
                    ApprovalTask.matrix_code == matrixCode,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _recordApprovalRequestedEvent(
        self,
        session: AsyncSession,
        *,
        instanceId: int,
        task: ApprovalTask,
        subjectType: str,
        subjectId: str,
        dueAt: datetime | None,
    ) -> None:
        """落「审批已发起」事件（只在本轮**新建**时写）。"""

        await self.enqueueEvent(
            session,
            instanceId=instanceId,
            eventType=EVENT_APPROVAL_REQUESTED,
            payload={
                "taskId": task.id,
                "matrixCode": task.matrix_code,
                "subjectType": subjectType,
                "subjectId": subjectId,
                "dueAt": dueAt.isoformat() if dueAt is not None else None,
            },
        )

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
            "updated_time": utcnow(),
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
        eventType: str | None = None,
    ) -> ApprovalTask:
        """推进审批任务状态（不写签署人）—— 超时置 TIMEOUT / 取消置 CANCELLED 走这条。"""

        current = await self._lockApprovalTask(session, taskId)
        assertWorkflowTransition("approval", current.status, toStatus)

        values: dict[str, Any] = {"status": toStatus, "updated_time": utcnow()}
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
                eventType
                or (EVENT_APPROVAL_TIMED_OUT if toStatus == "TIMEOUT" else EVENT_APPROVAL_DECIDED)
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
