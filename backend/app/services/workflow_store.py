"""P5 工作流存储层（**门面**）：写操作收敛的唯一入口。

**为什么是门面**：存储层按职责域拆成四个子存储 + 一个共享底座，单文件都在行数
上限（800 行）内，且「谁写哪张表」一眼可见：

======================  ====================================================
模块                    职责
======================  ====================================================
``workflow_store_base``   共享底座：状态字面量 / 事件词汇表 / 条件 UPDATE
``workflow_instance_store``  实例 + 步骤（``workflow_instance`` / ``workflow_step``）
``workflow_command_store``   业务命令（``business_command``）
``workflow_approval_store``  审批任务（``approval_task``）
``workflow_definition_store`` 工作流定义（``workflow_definition``）
``workflow_store``（本文件） 门面：把四者组合成一个入口
======================  ====================================================

**调用方无须知道拆分**：门面同时继承四个子存储 ⇒ 方法名与拆分前**逐字一致**
⇒ 调用点零改动。

三条纪律（对应实施计划 D-2 / D-3 / D-4，详见 ``workflow_store_base.py``）：

1. **幂等**（D-2）：``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` ⇒
   重复提交只落一行；命中冲突返回既有行且 ``created=False``（**不是错误**）。
2. **行锁 + 重读**（D-3）：推进前 ``SELECT ... FOR UPDATE`` +
   ``populate_existing=True`` ⇒ 并发下不丢步。
3. **条件 UPDATE claim**（D-3）：``WHERE id=? AND status='PENDING'`` ⇒
   同一命令只能被 claim 一次；候选选取用 ``SKIP LOCKED``。

**不 commit**：所有写操作与调用方业务写在同一事务里原子提交。
"""

from __future__ import annotations

from app.services.workflow_approval_store import WorkflowApprovalStore
from app.services.workflow_command_store import WorkflowCommandStore
from app.services.workflow_definition_store import WorkflowDefinitionStore
from app.services.workflow_instance_store import WorkflowInstanceStore
from app.services.workflow_store_base import (
    EVENT_APPROVAL_CANCELLED,
    EVENT_APPROVAL_DECIDED,
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_TIMED_OUT,
    EVENT_COMMAND_ABORTED,
    EVENT_COMMAND_CANCELLED,
    EVENT_COMMAND_CLAIMED,
    EVENT_COMMAND_COMPLETED,
    EVENT_COMMAND_FAILED,
    EVENT_COMMAND_REQUESTED,
    EVENT_INSTANCE_CREATED,
    EVENT_INSTANCE_STATUS_CHANGED,
    EVENT_STEP_STATUS_CHANGED,
    EVENT_STEPS_CREATED,
)

__all__ = [
    "WorkflowStore",
    "EVENT_APPROVAL_CANCELLED",
    "EVENT_APPROVAL_DECIDED",
    "EVENT_APPROVAL_REQUESTED",
    "EVENT_APPROVAL_TIMED_OUT",
    "EVENT_COMMAND_ABORTED",
    "EVENT_COMMAND_CANCELLED",
    "EVENT_COMMAND_CLAIMED",
    "EVENT_COMMAND_COMPLETED",
    "EVENT_COMMAND_FAILED",
    "EVENT_COMMAND_REQUESTED",
    "EVENT_INSTANCE_CREATED",
    "EVENT_INSTANCE_STATUS_CHANGED",
    "EVENT_STEP_STATUS_CHANGED",
    "EVENT_STEPS_CREATED",
]


class WorkflowStore(
    WorkflowInstanceStore,
    WorkflowCommandStore,
    WorkflowApprovalStore,
    WorkflowDefinitionStore,
):
    """存储层门面：实例+步骤 / 命令 / 审批 / 定义 四块拼成一个入口。

    用**多继承**而不是逐个转发方法 ⇒ 方法名与拆分前逐字一致 ⇒ 调用点零改动，
    也不会出现「转发层与实现层签名漂移」。
    """
