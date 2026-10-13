"""P1-2: cost_source 列（renumbered 0115→0126 to align with main alembic head）

Revision ID: 0126
Revises: 0125
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_token_usage",
        sa.Column("cost_source", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_token_usage", "cost_source")
