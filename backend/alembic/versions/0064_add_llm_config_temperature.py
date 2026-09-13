"""Add temperature to llm_config.

Revision ID: 0064
Revises: 0063
Create Date: 2026-09-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0064_add_llm_config_temperature"
down_revision = "0063_wiki_compile_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_config",
        sa.Column("temperature", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_config", "temperature")
