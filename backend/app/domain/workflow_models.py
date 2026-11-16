"""P5 工作流域的 ORM 映射（Workflow / Command / Approval）。

契约来源：`docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md` §6 P5，
实施计划：`Harness/changes/2026-11-16-p5-workflow-command/plan.md`。

六张表，分三组：

- **定义与实例**（0149）：`workflow_definition`（工作流定义，审批矩阵等纯数据放在
  ``definition_json``）/ `workflow_instance`（一次执行的实例）。
- **步骤与命令**（0150）：`workflow_step`（实例内的步骤）/ `business_command`
  （**写操作的唯一入口**；``idempotency_key`` 独立唯一列 ⇒ 重复提交只执行一次）。
- **审批与 outbox**（0151）：`approval_task`（审批任务，被审对象用**开放引用**
  `subject_type` + `subject_id`）/ `workflow_outbox`（异步事件，**不共用**审计 outbox）。

三条贯穿全族的设计纪律：

1. **不可变数据**：`workflow_definition` / `workflow_step` / `approval_task` /
   `business_command` 的状态变化都是**状态机驱动的整行替换**，不做局部原地改；
   状态机本身是纯函数矩阵（`app/services/workflow_state_machine.py`），
   库层用 CHECK 把字面量钉死（未知状态**入不了库** ⇒ 失败关闭）。
2. **写操作全部经 Command**：除 Command 执行器与其适配层外，任何服务层不得直接写
   `workflow_*` 表 ⇒ 由源码级反向守卫钉住（`test_workflow_write_gate.py`）。
3. **证据不悬空**：所有外键 `RESTRICT` —— 实例 / 步骤 / 审批都是审计证据，
   不允许因为父行被删而留下孤儿。

与迁移的对应：`alembic/versions/0149_workflow_definition.py` /
`0150_workflow_step_command.py` / `0151_approval_outbox.py`。
列注释与 CHECK 表达式两边**逐字一致**（单测 `test_workflow_models.py` 双向钉住）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, TimestampMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- 定义与实例

_DEFINITION_DOMAIN_COMMENT = "0149，P5：业务域编码（如 procurement）"
_DEFINITION_CAPABILITY_COMMENT = "0149，P5：能力编码（如 purchase_order_approve）"
_DEFINITION_VERSION_COMMENT = "0149，P5：定义版本号（同一域能力下唯一）"
_DEFINITION_JSON_COMMENT = "0149，P5：定义体（审批矩阵 / 步骤编排等纯数据，JSON 对象）"
_DEFINITION_STATUS_COMMENT = "0149，P5：定义状态（DRAFT / PUBLISHED / DISABLED）"

_INSTANCE_DEFINITION_COMMENT = "0149，P5：所属定义（workflow_definition.id）"
_INSTANCE_IDEMPOTENCY_COMMENT = "0149，P5：幂等键（同一定义下唯一；重复提交只执行一次）"
_INSTANCE_BUSINESS_KEY_COMMENT = "0149，P5：业务键（业务侧单号，可空）"
_INSTANCE_STATUS_COMMENT = "0149，P5：实例状态（状态机字面量集合）"
_INSTANCE_CURRENT_STEP_COMMENT = "0149，P5：当前步骤编码（workflow_step.step_code）"
_INSTANCE_REQUEST_COMMENT = "0149，P5：实例请求体（JSON 对象）"
_INSTANCE_RESULT_COMMENT = "0149，P5：实例结果体（JSON 对象，可空）"

# ---------------------------------------------------------------- 步骤与命令

_STEP_INSTANCE_COMMENT = "0150，P5：所属实例（workflow_instance.id）"
_STEP_CODE_COMMENT = "0150，P5：步骤编码（同一实例下唯一）"
_STEP_ORDER_COMMENT = "0150，P5：步骤序号（同一实例内递增，从 0 起）"
_STEP_STATUS_COMMENT = "0150，P5：步骤状态（状态机字面量集合）"
_STEP_INPUT_COMMENT = "0150，P5：步骤输入体（JSON 对象，可空）"
_STEP_OUTPUT_COMMENT = "0150，P5：步骤输出体（JSON 对象，可空）"

_COMMAND_INSTANCE_COMMENT = "0150，P5：所属实例（workflow_instance.id）"
_COMMAND_STEP_COMMENT = "0150，P5：所属步骤（可空：非步骤型命令）"
_COMMAND_TYPE_COMMENT = "0150，P5：命令类型（写操作的唯一入口类型）"
_COMMAND_IDEMPOTENCY_COMMENT = "0150，P5：幂等键（与 command_type 组成唯一约束；重复提交只执行一次）"
_COMMAND_STATUS_COMMENT = "0150，P5：命令状态（状态机字面量集合）"
_COMMAND_PAYLOAD_COMMENT = "0150，P5：命令载荷（JSON 对象）"
_COMMAND_RESULT_COMMENT = "0150，P5：命令执行结果（JSON 对象，可空）"
_COMMAND_ATTEMPTS_COMMENT = "0150，P5：已尝试次数（毒丸防护：超阈值不再重试）"

# ---------------------------------------------------------------- 审批与 outbox

_APPROVAL_INSTANCE_COMMENT = "0151，P5：所属实例（workflow_instance.id）"
_APPROVAL_STEP_COMMENT = "0151，P5：所属步骤（可空）"
_APPROVAL_SUBJECT_TYPE_COMMENT = (
    "0151，P5：被审对象类型（开放模型：如 workflow_instance / ontology_proposal）"
)
_APPROVAL_SUBJECT_ID_COMMENT = "0151，P5：被审对象标识（字符串；与 subject_type 组成开放引用）"
_APPROVAL_MATRIX_CODE_COMMENT = "0151，P5：审批矩阵条目编码（定义体 approvalMatrix 内的条目 key）"
_APPROVAL_STATUS_COMMENT = "0151，P5：审批任务状态（状态机字面量集合）"
_APPROVAL_ROLE_COMMENT = "0151，P5：期望审批角色（矩阵条目声明；越权审批的判据）"
_APPROVAL_USER_COMMENT = "0151，P5：实际审批人"
_APPROVAL_DUE_AT_COMMENT = "0151，P5：审批超时时刻（超时守卫判据）"

_OUTBOX_EVENT_TYPE_COMMENT = "0151，P5：事件类型（worker 据此路由）"
_OUTBOX_PAYLOAD_COMMENT = "0151，P5：事件载荷（JSON 对象）"
_OUTBOX_STATUS_COMMENT = "0151，P5：outbox 事件状态"
_OUTBOX_ATTEMPTS_COMMENT = "0151，P5：已尝试次数（毒丸防护）"
_OUTBOX_AVAILABLE_AT_COMMENT = "0151，P5：可被领取时刻（重试退避）"
_OUTBOX_LEASE_OWNER_COMMENT = "0151，P5：租约持有者（worker 标识）"
_OUTBOX_LEASE_EXPIRES_AT_COMMENT = "0151，P5：租约到期时刻"
_OUTBOX_PROCESSED_AT_COMMENT = "0151，P5：处理完成时刻（NULL = 待处理）"


class WorkflowDefinition(Base, TimestampMixin):
    """工作流定义：版本化的**纯数据**（步骤编排 + 审批矩阵）。"""

    __tablename__ = "workflow_definition"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    domain_code: Mapped[str] = mapped_column(String(64), nullable=False, comment=_DEFINITION_DOMAIN_COMMENT)
    capability_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment=_DEFINITION_CAPABILITY_COMMENT
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False, comment=_DEFINITION_VERSION_COMMENT)
    definition_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::jsonb"),
        comment=_DEFINITION_JSON_COMMENT,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="DRAFT",
        server_default=sa_text("'DRAFT'"),
        comment=_DEFINITION_STATUS_COMMENT,
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "domain_code",
            "capability_code",
            "version",
            name="uq_workflow_definition_scope",
        ),
        CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','DISABLED')",
            name="ck_workflow_definition_status",
        ),
        CheckConstraint(
            "jsonb_typeof(definition_json) = 'object'",
            name="ck_workflow_definition_json",
        ),
        Index(
            "ix_workflow_definition_scope",
            "domain_code",
            "capability_code",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<WorkflowDefinition id={self.id} "
            f"{self.domain_code}/{self.capability_code}@{self.version} {self.status}>"
        )


class WorkflowInstance(Base, TimestampMixin):
    """工作流实例：一次执行的运行态（状态机驱动）。"""

    __tablename__ = "workflow_instance"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    definition_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_definition.id",
            ondelete="RESTRICT",
            name="fk_workflow_instance_definition",
        ),
        nullable=False,
        comment=_INSTANCE_DEFINITION_COMMENT,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment=_INSTANCE_IDEMPOTENCY_COMMENT
    )
    business_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment=_INSTANCE_BUSINESS_KEY_COMMENT
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PENDING",
        server_default=sa_text("'PENDING'"),
        comment=_INSTANCE_STATUS_COMMENT,
    )
    current_step: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=_INSTANCE_CURRENT_STEP_COMMENT
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::jsonb"),
        comment=_INSTANCE_REQUEST_COMMENT,
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment=_INSTANCE_RESULT_COMMENT
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "definition_id",
            "idempotency_key",
            name="uq_workflow_instance_idempotency",
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','WAITING_APPROVAL','APPROVED',"
            "'REJECTED','COMPLETED','FAILED','CANCELLED','COMPENSATED')",
            name="ck_workflow_instance_status",
        ),
        CheckConstraint(
            "jsonb_typeof(request_json) = 'object'",
            name="ck_workflow_instance_request",
        ),
        Index("ix_workflow_instance_definition", "definition_id"),
        Index("ix_workflow_instance_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<WorkflowInstance id={self.id} def={self.definition_id} {self.status} step={self.current_step}>"
        )


class WorkflowStep(Base, TimestampMixin):
    """实例内的步骤（状态机驱动；步骤定义来自 definition_json 的编排）。"""

    __tablename__ = "workflow_step"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_instance.id",
            ondelete="RESTRICT",
            name="fk_workflow_step_instance",
        ),
        nullable=False,
        comment=_STEP_INSTANCE_COMMENT,
    )
    step_code: Mapped[str] = mapped_column(String(64), nullable=False, comment=_STEP_CODE_COMMENT)
    step_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0"), comment=_STEP_ORDER_COMMENT
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PENDING",
        server_default=sa_text("'PENDING'"),
        comment=_STEP_STATUS_COMMENT,
    )
    input_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment=_STEP_INPUT_COMMENT
    )
    output_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment=_STEP_OUTPUT_COMMENT
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("instance_id", "step_code", name="uq_workflow_step_code"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SKIPPED','COMPENSATED')",
            name="ck_workflow_step_status",
        ),
        CheckConstraint("step_order >= 0", name="ck_workflow_step_order"),
        Index("ix_workflow_step_instance", "instance_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<WorkflowStep id={self.id} instance={self.instance_id} "
            f"{self.step_code}#{self.step_order} {self.status}>"
        )


class BusinessCommand(Base, TimestampMixin):
    """业务命令：**写操作的唯一入口**。

    ``idempotency_key`` 与 ``command_type`` 组成唯一约束 ⇒ 重复提交只执行一次
    （D-2：独立唯一列，不复用 JSONB 里的键）。
    """

    __tablename__ = "business_command"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_instance.id",
            ondelete="RESTRICT",
            name="fk_business_command_instance",
        ),
        nullable=False,
        comment=_COMMAND_INSTANCE_COMMENT,
    )
    step_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_step.id",
            ondelete="RESTRICT",
            name="fk_business_command_step",
        ),
        nullable=True,
        comment=_COMMAND_STEP_COMMENT,
    )
    command_type: Mapped[str] = mapped_column(String(64), nullable=False, comment=_COMMAND_TYPE_COMMENT)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment=_COMMAND_IDEMPOTENCY_COMMENT
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PENDING",
        server_default=sa_text("'PENDING'"),
        comment=_COMMAND_STATUS_COMMENT,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::jsonb"),
        comment=_COMMAND_PAYLOAD_COMMENT,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment=_COMMAND_RESULT_COMMENT
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=sa_text("0"),
        comment=_COMMAND_ATTEMPTS_COMMENT,
    )
    executed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "command_type",
            "idempotency_key",
            name="uq_business_command_idempotency",
        ),
        CheckConstraint(
            "status IN ('PENDING','EXECUTING','SUCCEEDED','FAILED','COMPENSATED')",
            name="ck_business_command_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_business_command_attempts"),
        Index("ix_business_command_instance", "instance_id"),
        Index("ix_business_command_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<BusinessCommand id={self.id} {self.command_type} {self.status} attempts={self.attempts}>"


class ApprovalTask(Base, TimestampMixin):
    """审批任务。

    **开放对象模型**（D-1）：被审对象用 ``subject_type`` + ``subject_id`` 引用，
    不写死成工作流实例 ⇒ WP-ONT-AI A4 的「四眼审核」可直接复用这张表。
    ``matrix_code`` 指向定义体 ``approvalMatrix`` 里的条目 key。
    """

    __tablename__ = "approval_task"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_instance.id",
            ondelete="RESTRICT",
            name="fk_approval_task_instance",
        ),
        nullable=False,
        comment=_APPROVAL_INSTANCE_COMMENT,
    )
    step_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_step.id",
            ondelete="RESTRICT",
            name="fk_approval_task_step",
        ),
        nullable=True,
        comment=_APPROVAL_STEP_COMMENT,
    )
    subject_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment=_APPROVAL_SUBJECT_TYPE_COMMENT
    )
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, comment=_APPROVAL_SUBJECT_ID_COMMENT)
    matrix_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment=_APPROVAL_MATRIX_CODE_COMMENT
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PENDING",
        server_default=sa_text("'PENDING'"),
        comment=_APPROVAL_STATUS_COMMENT,
    )
    approver_role: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=_APPROVAL_ROLE_COMMENT
    )
    approver_user: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=_APPROVAL_USER_COMMENT
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_APPROVAL_DUE_AT_COMMENT
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("instance_id", "matrix_code", name="uq_approval_task_matrix"),
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','CANCELLED','TIMEOUT')",
            name="ck_approval_task_status",
        ),
        Index("ix_approval_task_instance", "instance_id"),
        Index("ix_approval_task_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<ApprovalTask id={self.id} instance={self.instance_id} {self.matrix_code} {self.status}>"


class WorkflowOutbox(Base):
    """工作流 outbox（**不共用**审计 outbox 表，D-5）。

    结构照抄 ``AuditOutbox`` 的毒丸防护：``attempts`` / ``last_error`` /
    ``processed_at``；另加租约字段（``lease_owner`` / ``lease_expires_at``）与
    ``available_at``（重试退避）⇒ Worker 崩溃后租约到期即可被重新领取。
    """

    __tablename__ = "workflow_outbox"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    instance_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "workflow_instance.id",
            ondelete="RESTRICT",
            name="fk_workflow_outbox_instance",
        ),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, comment=_OUTBOX_EVENT_TYPE_COMMENT)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa_text("'{}'::jsonb"),
        comment=_OUTBOX_PAYLOAD_COMMENT,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="PENDING",
        server_default=sa_text("'PENDING'"),
        comment=_OUTBOX_STATUS_COMMENT,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=sa_text("0"),
        comment=_OUTBOX_ATTEMPTS_COMMENT,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=sa_text("now()"),
        comment=_OUTBOX_AVAILABLE_AT_COMMENT,
    )
    lease_owner: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment=_OUTBOX_LEASE_OWNER_COMMENT
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_OUTBOX_LEASE_EXPIRES_AT_COMMENT
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment=_OUTBOX_PROCESSED_AT_COMMENT
    )
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','PROCESSED','FAILED')",
            name="ck_workflow_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_workflow_outbox_attempts"),
        Index("ix_workflow_outbox_pending", "status", "available_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkflowOutbox id={self.id} {self.event_type} {self.status} attempts={self.attempts}>"
