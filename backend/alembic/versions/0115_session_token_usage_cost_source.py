"""P1-2: cost_source 列

Revision ID: 0115
Revises: 0114
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_token_usage",
        sa.Column("cost_source", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_token_usage", "cost_source")
