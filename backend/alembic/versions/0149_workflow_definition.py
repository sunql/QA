"""P5 工作流域：`workflow_definition` / `workflow_instance`（0149）。

契约来源 `docs/update/1109/智能采购平台评估与渐进升级计划_跨板块扩展_1109.md` §6 P5；
实施计划 `Harness/changes/2026-11-16-p5-workflow-command/plan.md`（W0 / D-2 / D-4）。

- `workflow_definition`：**版本化的纯数据**。步骤编排与**采购审批矩阵**放在
  `definition_json` 里 ⇒ 矩阵变更只改数据，不改代码、不改表结构。
- `workflow_instance`：一次执行的运行态。`idempotency_key` 是**独立唯一列**
  （D-2：JSONB 里的键做不了唯一约束 ⇒ 并发下「重复提交只执行一次」保证不住），
  作用域 = `(definition_id, created_by, idempotency_key)` ⇒ **幂等键按提交人
  私有**：① 别人的键不会被「占死」（DoS）；② 复用别人的键只拿到**自己的**
  实例（不泄露别人的实例、也不泄露「这个键存在」）。

  ⚠️ 作用域里的 `created_by` **NOT NULL**：PG 里 NULL 互不相等 ⇒ 该列可空时
  幂等保证会**静默失效**（同一个键能落两行 ⇒ 重复执行）。

约束各有明确目的：

- `uq_workflow_definition_scope`：同一域能力下版本号唯一。
- `uq_workflow_instance_idempotency`：重复提交只落一条实例（按提交人隔离）。
- `ck_workflow_definition_status` / `ck_workflow_instance_status`：
  状态字面量钉死在库侧（状态机是纯函数矩阵，未知状态**入不了库** ⇒ 失败关闭）。
- `ck_workflow_definition_json` / `ck_workflow_instance_request`：
  JSON 列必须是对象（塞成数组/字符串 ⇒ 解析侧会炸）。
- FK `fk_workflow_instance_definition` 为 `RESTRICT`：定义是审计证据，不允许悬空。

**手写**（禁 autogenerate）：纯加法 DDL，只建两张新表，不触碰任何既有表。
列 `comment` 与 CHECK 表达式与 `app/domain/workflow_models.py` 的 ORM 声明
**逐字一致**（单测 `app/tests/unit/test_workflow_models.py` 双向钉住）。

**幂等性**：本迁移只建新表；表名固定，`downgrade()` 对称撤销。

**修订策略**：本迁移随 PR 落地、尚未在任何持久环境执行 ⇒ 评审期补的约束
原地加在本 revision 里，不新开迁移。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None

_TABLE_DEFINITION = "workflow_definition"
_TABLE_INSTANCE = "workflow_instance"

# 与 `app/domain/workflow_models.py` 的 ORM 列注释逐字一致（单测双向钉住）。
COLUMN_COMMENTS = {
    "definition_domain_code": "0149，P5：业务域编码（如 procurement）",
    "definition_capability_code": "0149，P5：能力编码（如 purchase_order_approve）",
    "definition_version": "0149，P5：定义版本号（同一域能力下唯一）",
    "definition_json": "0149，P5：定义体（审批矩阵 / 步骤编排等纯数据，JSON 对象）",
    "definition_status": "0149，P5：定义状态（DRAFT / PUBLISHED / DISABLED）",
    "instance_definition_id": "0149，P5：所属定义（workflow_definition.id）",
    "instance_idempotency_key": "0149，P5：幂等键（同一定义+同一提交人下唯一；重复提交只执行一次）",
    "instance_business_key": "0149，P5：业务键（业务侧单号，可空）",
    "instance_status": "0149，P5：实例状态（状态机字面量集合）",
    "instance_request_json": "0149，P5：实例请求体（JSON 对象）",
    "instance_result_json": "0149，P5：实例结果体（JSON 对象，可空）",
    "instance_created_by": "0149，P5：提交人（幂等作用域的一环 ⇒ NOT NULL）",
}

# 与 ORM 逐字一致的 CHECK 表达式。
CHECK_EXPRESSIONS = {
    "definition_status": "status IN ('DRAFT','PUBLISHED','DISABLED')",
    "definition_json": "jsonb_typeof(definition_json) = 'object'",
    "instance_status": (
        "status IN ('PENDING','RUNNING','WAITING_APPROVAL','APPROVED',"
        "'REJECTED','COMPLETED','FAILED','CANCELLED','COMPENSATED')"
    ),
    "instance_request": "jsonb_typeof(request_json) = 'object'",
}


def upgrade() -> None:
    op.create_table(
        _TABLE_DEFINITION,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "domain_code",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["definition_domain_code"],
        ),
        sa.Column(
            "capability_code",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["definition_capability_code"],
        ),
        sa.Column(
            "version",
            sa.String(length=32),
            nullable=False,
            comment=COLUMN_COMMENTS["definition_version"],
        ),
        sa.Column(
            "definition_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=COLUMN_COMMENTS["definition_json"],
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'DRAFT'"),
            comment=COLUMN_COMMENTS["definition_status"],
        ),
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
        sa.UniqueConstraint(
            "domain_code",
            "capability_code",
            "version",
            name="uq_workflow_definition_scope",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["definition_status"],
            name="ck_workflow_definition_status",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["definition_json"],
            name="ck_workflow_definition_json",
        ),
    )
    op.create_index(
        "ix_workflow_definition_scope",
        _TABLE_DEFINITION,
        ["domain_code", "capability_code"],
    )

    op.create_table(
        _TABLE_INSTANCE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "definition_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["instance_definition_id"],
        ),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
            comment=COLUMN_COMMENTS["instance_idempotency_key"],
        ),
        sa.Column(
            "business_key",
            sa.String(length=128),
            nullable=True,
            comment=COLUMN_COMMENTS["instance_business_key"],
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'PENDING'"),
            comment=COLUMN_COMMENTS["instance_status"],
        ),
        sa.Column(
            "request_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=COLUMN_COMMENTS["instance_request_json"],
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(),
            nullable=True,
            comment=COLUMN_COMMENTS["instance_result_json"],
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["instance_created_by"],
        ),
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
            ["definition_id"],
            ["workflow_definition.id"],
            name="fk_workflow_instance_definition",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "definition_id",
            "created_by",
            "idempotency_key",
            name="uq_workflow_instance_idempotency",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["instance_status"],
            name="ck_workflow_instance_status",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["instance_request"],
            name="ck_workflow_instance_request",
        ),
    )
    op.create_index(
        "ix_workflow_instance_definition",
        _TABLE_INSTANCE,
        ["definition_id"],
    )
    op.create_index(
        "ix_workflow_instance_status",
        _TABLE_INSTANCE,
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_instance_status", table_name=_TABLE_INSTANCE)
    op.drop_index("ix_workflow_instance_definition", table_name=_TABLE_INSTANCE)
    op.drop_table(_TABLE_INSTANCE)
    op.drop_index("ix_workflow_definition_scope", table_name=_TABLE_DEFINITION)
    op.drop_table(_TABLE_DEFINITION)
