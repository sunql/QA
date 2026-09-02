"""add tool_name + tool_name_updated_at to agent_definition

Revision ID: 0035_agent_tool_binding
Revises: 0034_entity_mapping_name_index
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_agent_tool_binding"
down_revision = "0034_entity_mapping_name_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_definition",
        sa.Column("tool_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_definition",
        sa.Column(
            "tool_name_updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_definition", "tool_name_updated_at")
    op.drop_column("agent_definition", "tool_name")
