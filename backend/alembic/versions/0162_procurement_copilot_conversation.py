"""I3 多轮 Copilot：会话壳 + 轮次映射（设计稿 §8.4 的统一对话入口）。

为什么不往 `agent_orchestration_run` 加列：那是编排域共享表，会话是一个
**独立的归属概念**（谁开的会话、绑哪个 Agent/模型、多少轮），混进 run 会把
会话语义反向污染到所有编排运行。

Revision ID: 0162
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0162"
down_revision = "0161"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procurement_copilot_conversation",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("agent_code", sa.String(64), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "uq_copilot_conversation_id",
        "procurement_copilot_conversation",
        ["conversation_id"],
        unique=True,
    )
    op.create_index(
        "ix_copilot_conversation_owner",
        "procurement_copilot_conversation",
        ["created_by"],
    )

    op.create_table(
        "procurement_copilot_turn",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column(
            "created_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_time", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_copilot_turn_seq"),
    )
    op.create_index("ix_copilot_turn_run", "procurement_copilot_turn", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_copilot_turn_run", table_name="procurement_copilot_turn")
    op.drop_table("procurement_copilot_turn")
    op.drop_index(
        "ix_copilot_conversation_owner",
        table_name="procurement_copilot_conversation",
    )
    op.drop_index(
        "uq_copilot_conversation_id",
        table_name="procurement_copilot_conversation",
    )
    op.drop_table("procurement_copilot_conversation")
