"""add agent_tool_config table (DB-backed tool registry SSOT)

Revision ID: 0037_agent_tool_config
Revises: 0036_audit_actor_index
Create Date: 2026-09-03

Why: feat-agent-tool-config-db 把硬编码 agent_tool_registry
（_buildRegistry() / agent_tool_registry / AGENT_DEFAULT_BINDINGS）
迁到 DB SSOT。name 与 AgentDefinition.tool_name 形成 FK-by-name 引用，
不可改。handler_kind 用 CHECK 约束兜底（service 层校验之外）。

新表：agent_tool_config（13 列 + 1 unique + 1 CHECK + 2 index）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0037_agent_tool_config"
down_revision = "0036_audit_actor_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tool_config",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data_object", sa.String(length=128), nullable=False),
        sa.Column(
            "data_layers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("handler_kind", sa.String(length=30), nullable=False),
        sa.Column("handler_ref", sa.String(length=64), nullable=False),
        sa.Column(
            "arg_extractor_kind",
            sa.String(length=64),
            nullable=False,
            server_default="supplier_key",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_agent_tool_config_name"),
        sa.CheckConstraint(
            "handler_kind IN ('BUILTIN','NL2SQL')",
            name="ck_agent_tool_config_handler_kind",
        ),
    )
    op.create_index(
        "ix_agent_tool_config_enabled", "agent_tool_config", ["enabled"]
    )
    op.create_index(
        "ix_agent_tool_config_data_object", "agent_tool_config", ["data_object"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_tool_config_data_object", table_name="agent_tool_config")
    op.drop_index("ix_agent_tool_config_enabled", table_name="agent_tool_config")
    op.drop_table("agent_tool_config")