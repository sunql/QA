"""P5 存储层的**共享底座**：状态字面量 / 事件词汇表 / 通用写操作。

为什么单独成文件：存储层按职责域拆成三个子存储（实例+步骤 / 命令 / 审批），
它们共用同一套**状态字面量**、**事件词汇表**与**条件 UPDATE**写法 ⇒ 放一份在
底座里，避免三处各写一遍、慢慢漂移。

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

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.workflow_models import WorkflowInstance, WorkflowOutbox
from app.services.workflow_state_machine import LIVE_INSTANCE_STATUSES

# 命令 / 步骤的初始状态字面量（与状态机矩阵、库层 CHECK 一致）。
STATUS_PENDING = "PENDING"
STATUS_EXECUTING = "EXECUTING"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_ABORTED = "ABORTED"

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
EVENT_COMMAND_ABORTED = "command_aborted"
EVENT_COMMAND_CANCELLED = "command_cancelled"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_DECIDED = "approval_decided"
EVENT_APPROVAL_TIMED_OUT = "approval_timed_out"
EVENT_APPROVAL_CANCELLED = "approval_cancelled"

# 事件载荷里的错误摘要长度上限（错误全文已落 business_command.error_message）。
MAX_EVENT_ERROR_LEN = 500

# 幂等冲突的错误信息（ConflictError ⇒ 409）。
MSG_STEP_INSTANCE_MISMATCH = "步骤 {stepId} 不属于实例 {instanceId}（拒绝跨实例挂载）"


def utcnow() -> datetime:
    return datetime.now(UTC)


def liveInstanceIds() -> Any:
    """「还在跑」的实例 id 子查询（可领取命令的前提条件）。

    ⚠️ 用子查询而不是先查再比：并发下「先查后比」会有窗口（查到活实例，
    随后实例被取消，再去领命令）⇒ 子查询让**判定与写入在同一条语句里**完成。
    """

    return select(WorkflowInstance.id).where(
        WorkflowInstance.status.in_(LIVE_INSTANCE_STATUSES)
    )


class WorkflowStoreBase:
    """存储层底座：事件写入 + 条件 UPDATE（三个子存储的共同依赖）。

    **不 commit**：所有写操作与调用方业务写在同一事务里原子提交（照
    `app/services/outbox_service.py` 的同事务语义）。

    **同时是「每步可追溯」的唯一入口**：所有状态流转都在这里写一条
    ``workflow_outbox`` 事件（同事务）⇒ 事件流不可能被绕过。
    """


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
