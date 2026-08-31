"""agent_definition + agent_access_policy（Phase 6.1）。

Agent 注册表（agent_definition）：存储 AI Agent 的元数据：唯一编码、名称、
描述、触发类型（USER_QUESTION / SCHEDULED / EVENT）、响应延迟（REALTIME /
BATCH）、数据域与数据层（JSONB 数组）、治理状态（active / draft /
deprecated）、归属部门、版本。

Agent 访问策略表（agent_access_policy）：每个 Agent 对各数据对象（SUPPLIER /
PURCHASE_ORDER / MATERIAL ...）在各数据层（DIM / DWD / DWS / FEATURE / ADS）
的访问权限（read / masked_read / forbidden / forbidden_write）。同一 Agent
对同一 data_object + data_layer 至多一条策略。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031_agent_registry"
down_revision: str | None = "0030_document_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # agent_definition
    # -------------------------------------------------------------------------
    op.create_table(
        "agent_definition",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_code", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "trigger_type",
            sa.String(length=30),
            nullable=False,
            server_default="user_question",
        ),
        sa.Column(
            "response_latency",
            sa.String(length=30),
            nullable=False,
            server_default="realtime",
        ),
        sa.Column(
            "data_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "data_layers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("owner", sa.String(length=128), nullable=True),
        sa.Column(
            "version", sa.String(length=32), nullable=False, server_default="v1.0"
        ),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_code", name="uq_agent_code"),
        sa.CheckConstraint(
            "trigger_type IN ('user_question','scheduled','event')",
            name="ck_agent_trigger_type",
        ),
        sa.CheckConstraint(
            "response_latency IN ('realtime','batch')",
            name="ck_agent_response_latency",
        ),
        sa.CheckConstraint(
            "status IN ('active','draft','deprecated')",
            name="ck_agent_status",
        ),
    )
    op.create_index("ix_agent_definition_status", "agent_definition", ["status"])
    op.create_index(
        "ix_agent_definition_owner", "agent_definition", ["owner"]
    )

    # -------------------------------------------------------------------------
    # agent_access_policy
    # -------------------------------------------------------------------------
    op.create_table(
        "agent_access_policy",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "agent_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_definition.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("data_object", sa.String(length=128), nullable=False),
        sa.Column(
            "permission",
            sa.String(length=30),
            nullable=False,
            server_default="read",
        ),
        sa.Column("data_layer", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id", "data_object", "data_layer",
            name="uq_agent_policy_object_layer",
        ),
        sa.CheckConstraint(
            "permission IN ('read','masked_read','forbidden','forbidden_write')",
            name="ck_agent_permission",
        ),
    )
    op.create_index(
        "ix_agent_policy_agent", "agent_access_policy", ["agent_id"]
    )
    op.create_index(
        "ix_agent_policy_object",
        "agent_access_policy",
        ["data_object", "data_layer"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_policy_object", table_name="agent_access_policy")
    op.drop_index("ix_agent_policy_agent", table_name="agent_access_policy")
    op.drop_table("agent_access_policy")
    op.drop_index("ix_agent_definition_owner", table_name="agent_definition")
    op.drop_index("ix_agent_definition_status", table_name="agent_definition")
    op.drop_table("agent_definition")