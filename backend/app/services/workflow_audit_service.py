"""P5 治理审计写入（**audit_log**）。

两条审计面（刻意分工）：

- **每步可追溯** = ``workflow_outbox`` 事件流 —— 由 ``WorkflowStore`` 在
  每次状态流转时同事务写入（见 ``workflow_store.enqueueEvent``）。
  ⚠️ 该表当前**没有机器消费者**（Worker 轮询的是 ``business_command``）⇒
  事件流是**给人看 / 查 SQL** 的追溯记录。
- **治理审计** = 本模块 —— 「**谁**做了什么」：提交 / 发起审批 / 通过 /
  拒绝 / 超时 / 补偿。

两条纪律：

1. **只 add 不 commit**：与业务写同事务 ⇒ 业务回滚时审计一起回滚
   （不会出现「业务失败但审计说成功」）。
2. **动作走白名单**：``AuditService._VALID_ACTIONS`` + DB CHECK 约束
   （迁移 0152 同步放宽）⇒ 动作拼错在服务层就炸，不会静默丢审计。

⚠️ 审计写入**不是**「可选的副产物」：审批是写入口（能推进审批 = 能放行
业务写）⇒ 决策必须留痕。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import ApprovalTask, WorkflowInstance
from app.services.actor_departments import DepartmentsInput
from app.services.audit_service import AuditService

# 审计动作（与 audit_service._VALID_ACTIONS、DB CHECK 约束三处一致）。
ACTION_SUBMIT = "WORKFLOW_SUBMIT"
ACTION_APPROVAL_REQUESTED = "WORKFLOW_APPROVAL_REQUESTED"
ACTION_APPROVE = "APPROVE"
ACTION_REJECT = "REJECT"
ACTION_TIMEOUT = "WORKFLOW_TIMEOUT"
ACTION_COMPENSATE = "COMPENSATE"
ACTION_CANCEL = "WORKFLOW_CANCEL"

# 审计实体类型（与 audit_log.entity_type 一致）。
ENTITY_INSTANCE = "workflow_instance"
ENTITY_APPROVAL_TASK = "approval_task"


class WorkflowAuditService:
    """P5 治理审计写入器。"""

    def __init__(self, audit: AuditService | None = None) -> None:
        self._audit = audit or AuditService()

    async def recordSubmit(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        actor: str,
        actorDepartments: DepartmentsInput = None,
    ) -> None:
        """提交工作流实例。"""

        await self._audit.record(
            session,
            entity_type=ENTITY_INSTANCE,
            entity_id=instance.id,
            action=ACTION_SUBMIT,
            actor=actor,
            actor_departments=actorDepartments,
            after={
                "status": instance.status,
                "definitionId": instance.definition_id,
                "idempotencyKey": instance.idempotency_key,
            },
        )

    async def recordApprovalRequested(
        self,
        session: AsyncSession,
        *,
        task: ApprovalTask,
        actor: str,
        actorDepartments: DepartmentsInput = None,
    ) -> None:
        """发起一轮审批（生成审批任务）。"""

        await self._audit.record(
            session,
            entity_type=ENTITY_APPROVAL_TASK,
            entity_id=task.id,
            action=ACTION_APPROVAL_REQUESTED,
            actor=actor,
            actor_departments=actorDepartments,
            after={
                "status": task.status,
                "matrixCode": task.matrix_code,
                "subjectType": task.subject_type,
                "subjectId": task.subject_id,
                "instanceId": task.instance_id,
            },
        )

    async def recordDecision(
        self,
        session: AsyncSession,
        *,
        task: ApprovalTask,
        decision: str,
        actor: str,
        beforeStatus: str,
        beforeApproverUser: str | None = None,
        note: str | None = None,
        actorDepartments: DepartmentsInput = None,
    ) -> None:
        """审批决策（APPROVE / REJECT）。

        ⚠️ ``before`` 必须是**决策前**的快照 ⇒ 签署人由调用方在更新**之前**
        取好传进来（``beforeApproverUser``）⇒ 不能从**更新后**的任务行反推
        （那会让「变更前」里出现「变更后」的值）。
        """

        action = ACTION_APPROVE if decision == "APPROVE" else ACTION_REJECT
        await self._audit.record(
            session,
            entity_type=ENTITY_APPROVAL_TASK,
            entity_id=task.id,
            action=action,
            actor=actor,
            actor_departments=actorDepartments,
            before={"status": beforeStatus, "approverUser": beforeApproverUser},
            after={
                "status": task.status,
                "approverUser": task.approver_user,
                "note": note,
            },
        )

    async def recordTimeout(
        self,
        session: AsyncSession,
        *,
        task: ApprovalTask,
        actor: str,
        beforeStatus: str | None = None,
    ) -> None:
        """审批超时（任务转 TIMEOUT、实例转 FAILED）。

        ⚠️ ``beforeStatus``：**真实的前态**（调用方在推进前取好）⇒ 不写死
        ``PENDING`` —— 写死就是「审计说的事和库里发生的事不是一回事」。
        """

        await self._audit.record(
            session,
            entity_type=ENTITY_APPROVAL_TASK,
            entity_id=task.id,
            action=ACTION_TIMEOUT,
            actor=actor,
            before={"status": beforeStatus or "PENDING"},
            after={"status": task.status},
        )

    async def recordCompensation(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        actor: str,
        reason: str,
        beforeStatus: str | None = None,
        succeededCount: int = 0,
        failedCount: int = 0,
        skippedCount: int = 0,
    ) -> None:
        """失败补偿（逆序回滚已成功的命令）。

        带上**补偿计数**：审计不能只说「已补偿」而不说补偿了几条 ——
        补偿失败时必须看得见（否则「补偿」与纸面动作无法区分）。

        ⚠️ ``beforeStatus``：**补偿前的真实实例状态**（多半是 ``RUNNING``）⇒
        不写死 ``FAILED`` —— 写死就是**假审计**（审计会声称实例补偿前已失败，
        而库里它当时还在跑）。
        """

        await self._audit.record(
            session,
            entity_type=ENTITY_INSTANCE,
            entity_id=instance.id,
            action=ACTION_COMPENSATE,
            actor=actor,
            before={"status": beforeStatus or "FAILED"},
            after={
                "status": instance.status,
                "reason": reason[:500],
                "succeededCount": succeededCount,
                "failedCount": failedCount,
                "skippedCount": skippedCount,
            },
        )

    async def recordCancel(
        self,
        session: AsyncSession,
        *,
        instance: WorkflowInstance,
        actor: str,
        beforeStatus: str,
        reason: str | None = None,
        actorDepartments: DepartmentsInput = None,
    ) -> None:
        """取消工作流实例（待审批 / 待执行命令一并取消 ⇒ 外部写不再发生）。"""

        await self._audit.record(
            session,
            entity_type=ENTITY_INSTANCE,
            entity_id=instance.id,
            action=ACTION_CANCEL,
            actor=actor,
            actor_departments=actorDepartments,
            before={"status": beforeStatus},
            after={
                "status": instance.status,
                "reason": (reason or "")[:500],
            },
        )
