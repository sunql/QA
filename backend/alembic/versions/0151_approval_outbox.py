"""P5 工作流域：`approval_task` / `workflow_outbox`（0151）。

契约来源 `docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md` §6 P5；
实施计划 `Harness/changes/2026-11-16-p5-workflow-command/plan.md`（W0 / D-1 / D-5）。

- `approval_task`：审批任务。**开放对象模型**（D-1）—— 被审对象用
  `subject_type` + `subject_id` 引用，不写死成工作流实例 ⇒ WP-ONT-AI A4 的
  「四眼审核」是同构需求，可直接复用这张表与同一套审批矩阵键。
  `matrix_code` 指向定义体 `approvalMatrix` 内的条目 key；`approver_role` 是
  **越权审批**的判据；`due_at` 是**超时**守卫的判据。
- `workflow_outbox`：**独立** outbox 表（契约明令「不共用表」）。结构照抄
  `AuditOutbox` 的毒丸防护（`attempts` / `last_error` / `processed_at`），
  另加租约字段（`lease_owner` / `lease_expires_at`）与 `available_at`（重试退避）
  ⇒ Worker 崩溃后租约到期即可被重新领取。

约束：

- `uq_approval_task_matrix`：同一实例同一矩阵条目只生成一条审批任务。
- `ck_approval_task_status` / `ck_workflow_outbox_status`：状态字面量钉死。
- `ck_workflow_outbox_attempts`：重试计数非负。
- FK 均 `RESTRICT`：实例 / 步骤是审计证据，不允许悬空。

**手写**（禁 autogenerate）：纯加法 DDL，只建两张新表，不触碰任何既有表。
列 `comment` 与 CHECK 表达式与 `app/domain/workflow_models.py` 的 ORM 声明
**逐字一致**（单测 `app/tests/unit/test_workflow_models.py` 双向钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None

_TABLE_APPROVAL = "approval_task"
_TABLE_OUTBOX = "workflow_outbox"

# 与 `app/domain/workflow_models.py` 的 ORM 列注释逐字一致（单测双向钉住）。
COLUMN_COMMENTS = {
    "approval_instance_id": "0151，P5：所属实例（workflow_instance.id）",
    "approval_step_id": "0151，P5：所属步骤（可空）",
    "approval_subject_type": "0151，P5：被审对象类型（开放模型：如 workflow_instance / ontology_proposal）",
    "approval_subject_id": "0151，P5：被审对象标识（字符串；与 subject_type 组成开放引用）",
    "approval_matrix_code": "0151，P5：审批矩阵条目编码（定义体 approvalMatrix 内的条目 key）",
    "approval_status": "0151，P5：审批任务状态（状态机字面量集合）",
    "approval_approver_role": "0151，P5：期望审批角色（矩阵条目声明；越权审批的判据）",
    "approval_approver_user": "0151，P5：实际审批人",
    "approval_due_at": "0151，P5：审批超时时刻（超时守卫判据）",
    "outbox_event_type": "0151，P5：事件类型（事件流词汇表；当前无消费者：只写不读）",
    "outbox_payload": "0151，P5：事件载荷（JSON 对象）",
    "outbox_status": "0151，P5：outbox 事件状态",
    "outbox_attempts": "0151，P5：已尝试次数（毒丸防护）",
    "outbox_available_at": "0151，P5：可被领取时刻（重试退避）",
    "outbox_lease_owner": "0151，P5：租约持有者（worker 标识）",
    "outbox_lease_expires_at": "0151，P5：租约到期时刻",
    "outbox_processed_at": "0151，P5：处理完成时刻（NULL = 待处理）",
}

# 与 ORM 逐字一致的 CHECK 表达式。
CHECK_EXPRESSIONS = {
    "approval_status": "status IN ('PENDING','APPROVED','REJECTED','CANCELLED','TIMEOUT')",
    "outbox_status": "status IN ('PENDING','PROCESSING','PROCESSED','FAILED')",
    "outbox_attempts": "attempts >= 0",
}


def upgrade() -> None:
    op.create_table(
        _TABLE_APPROVAL,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instance_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["approval_instance_id"],
        ),
        sa.Column(
            "step_id",
            sa.BigInteger(),
            nullable=True,
            comment=COLUMN_COMMENTS["approval_step_id"],
        ),
        sa.Column(
            "subject_type",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["approval_subject_type"],
        ),
        sa.Column(
            "subject_id",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["approval_subject_id"],
        ),
        sa.Column(
            "matrix_code",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["approval_matrix_code"],
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'PENDING'"),
            comment=COLUMN_COMMENTS["approval_status"],
        ),
        sa.Column(
            "approver_role",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["approval_approver_role"],
        ),
        sa.Column(
            "approver_user",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["approval_approver_user"],
        ),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["approval_due_at"],
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance.id"],
            name="fk_approval_task_instance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_step.id"],
            name="fk_approval_task_step",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "instance_id",
            "matrix_code",
            name="uq_approval_task_matrix",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["approval_status"],
            name="ck_approval_task_status",
        ),
    )
    op.create_index(
        "ix_approval_task_instance",
        _TABLE_APPROVAL,
        ["instance_id"],
    )
    op.create_index(
        "ix_approval_task_status",
        _TABLE_APPROVAL,
        ["status"],
    )

    op.create_table(
        _TABLE_OUTBOX,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["outbox_event_type"],
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=COLUMN_COMMENTS["outbox_payload"],
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'PENDING'"),
            comment=COLUMN_COMMENTS["outbox_status"],
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment=COLUMN_COMMENTS["outbox_attempts"],
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment=COLUMN_COMMENTS["outbox_available_at"],
        ),
        sa.Column(
            "lease_owner",
            sa.String(length=128),
            nullable=True,
            comment=COLUMN_COMMENTS["outbox_lease_owner"],
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["outbox_lease_expires_at"],
        ),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=COLUMN_COMMENTS["outbox_processed_at"],
        ),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["workflow_instance.id"],
            name="fk_workflow_outbox_instance",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["outbox_status"],
            name="ck_workflow_outbox_status",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["outbox_attempts"],
            name="ck_workflow_outbox_attempts",
        ),
    )
    op.create_index(
        "ix_workflow_outbox_pending",
        _TABLE_OUTBOX,
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_outbox_pending", table_name=_TABLE_OUTBOX)
    op.drop_table(_TABLE_OUTBOX)
    op.drop_index("ix_approval_task_status", table_name=_TABLE_APPROVAL)
    op.drop_index("ix_approval_task_instance", table_name=_TABLE_APPROVAL)
    op.drop_table(_TABLE_APPROVAL)
