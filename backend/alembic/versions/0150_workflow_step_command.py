"""P5 工作流域：`workflow_step` / `business_command`（0150）。

契约来源 `docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md` §6 P5；
实施计划 `Harness/changes/2026-11-16-p5-workflow-command/plan.md`（W0 / D-2 / D-3）。

- `workflow_step`：实例内的步骤。步骤**定义**来自 `workflow_definition.definition_json`
  的编排（纯数据），这张表只存**运行态**。
- `business_command`：**写操作的唯一入口**（契约：Agent 仍只读，所有写都经 Command）。

`business_command.idempotency_key` 是独立唯一列（D-2），作用域 =
`(command_type, idempotency_key)` ⇒ 重复提交只执行一次，且**并发**下也保证得住
（JSONB 里的键做不了唯一约束）。

约束：

- `uq_workflow_step_code`：同一实例下步骤编码唯一。
- `uq_business_command_idempotency`：写操作幂等的库级保证。
- `ck_workflow_step_status` / `ck_business_command_status`：状态字面量钉死
  （状态机是纯函数矩阵，未知状态**入不了库** ⇒ 失败关闭）。
- `ck_workflow_step_order` / `ck_business_command_attempts`：非负。
- 两个 FK 均 `RESTRICT`：实例 / 步骤是审计证据，不允许悬空。

**手写**（禁 autogenerate）：纯加法 DDL，只建两张新表，不触碰任何既有表。
列 `comment` 与 CHECK 表达式与 `app/domain/workflow_models.py` 的 ORM 声明
**逐字一致**（单测 `app/tests/unit/test_workflow_models.py` 双向钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None

_TABLE_STEP = "workflow_step"
_TABLE_COMMAND = "business_command"

# 与 `app/domain/workflow_models.py` 的 ORM 列注释逐字一致（单测双向钉住）。
COLUMN_COMMENTS = {
    "step_instance_id": "0150，P5：所属实例（workflow_instance.id）",
    "step_code": "0150，P5：步骤编码（同一实例下唯一）",
    "step_order": "0150，P5：步骤序号（同一实例内递增，从 0 起）",
    "step_status": "0150，P5：步骤状态（状态机字面量集合）",
    "step_input_json": "0150，P5：步骤输入体（JSON 对象，可空）",
    "step_output_json": "0150，P5：步骤输出体（JSON 对象，可空）",
    "command_instance_id": "0150，P5：所属实例（workflow_instance.id）",
    "command_step_id": "0150，P5：所属步骤（可空：非步骤型命令）",
    "command_type": "0150，P5：命令类型（写操作的唯一入口类型）",
    "command_idempotency_key": "0150，P5：幂等键（与 command_type 组成唯一约束；重复提交只执行一次）",
    "command_status": "0150，P5：命令状态（状态机字面量集合）",
    "command_payload": "0150，P5：命令载荷（JSON 对象）",
    "command_result": "0150，P5：命令执行结果（JSON 对象，可空）",
    "command_attempts": "0150，P5：已尝试次数（毒丸防护：超阈值不再重试）",
}

# 与 ORM 逐字一致的 CHECK 表达式。
CHECK_EXPRESSIONS = {
    "step_status": "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SKIPPED','COMPENSATED')",
    "step_order": "step_order >= 0",
    "command_status": (
        "status IN ('PENDING','EXECUTING','SUCCEEDED','FAILED','COMPENSATED','CANCELLED','ABORTED')"
    ),
    "command_attempts": "attempts >= 0",
}


def upgrade() -> None:
    op.create_table(
        _TABLE_STEP,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instance_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["step_instance_id"],
        ),
        sa.Column(
            "step_code",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["step_code"],
        ),
        sa.Column(
            "step_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment=COLUMN_COMMENTS["step_order"],
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'PENDING'"),
            comment=COLUMN_COMMENTS["step_status"],
        ),
        sa.Column(
            "input_json",
            postgresql.JSONB(),
            nullable=True,
            comment=COLUMN_COMMENTS["step_input_json"],
        ),
        sa.Column(
            "output_json",
            postgresql.JSONB(),
            nullable=True,
            comment=COLUMN_COMMENTS["step_output_json"],
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            name="fk_workflow_step_instance",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("instance_id", "step_code", name="uq_workflow_step_code"),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["step_status"],
            name="ck_workflow_step_status",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["step_order"],
            name="ck_workflow_step_order",
        ),
    )
    op.create_index(
        "ix_workflow_step_instance",
        _TABLE_STEP,
        ["instance_id"],
    )

    op.create_table(
        _TABLE_COMMAND,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instance_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["command_instance_id"],
        ),
        sa.Column(
            "step_id",
            sa.BigInteger(),
            nullable=True,
            comment=COLUMN_COMMENTS["command_step_id"],
        ),
        sa.Column(
            "command_type",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["command_type"],
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
            comment=COLUMN_COMMENTS["command_idempotency_key"],
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'PENDING'"),
            comment=COLUMN_COMMENTS["command_status"],
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=COLUMN_COMMENTS["command_payload"],
        ),
        sa.Column(
            "result",
            postgresql.JSONB(),
            nullable=True,
            comment=COLUMN_COMMENTS["command_result"],
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment=COLUMN_COMMENTS["command_attempts"],
        ),
        sa.Column("executed_by", sa.String(length=64), nullable=True),
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
            name="fk_business_command_instance",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_step.id"],
            name="fk_business_command_step",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "command_type",
            "idempotency_key",
            name="uq_business_command_idempotency",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["command_status"],
            name="ck_business_command_status",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["command_attempts"],
            name="ck_business_command_attempts",
        ),
    )
    op.create_index(
        "ix_business_command_instance",
        _TABLE_COMMAND,
        ["instance_id"],
    )
    op.create_index(
        "ix_business_command_status",
        _TABLE_COMMAND,
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_business_command_status", table_name=_TABLE_COMMAND)
    op.drop_index("ix_business_command_instance", table_name=_TABLE_COMMAND)
    op.drop_table(_TABLE_COMMAND)
    op.drop_index("ix_workflow_step_instance", table_name=_TABLE_STEP)
    op.drop_table(_TABLE_STEP)
