"""evidence payload + session_id

Revision ID: 0096
Revises: 0094
Create Date: 2026-09-28

Adds:
- evidence.payload JSONB (nullable; back-compat for Document type)
- evidence.session_id VARCHAR(64) (nullable; partial index on non-null)
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0096"
down_revision = "0094_wiki_page_category_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evidence", sa.Column("payload", JSONB(), nullable=True))
    op.add_column(
        "evidence",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_evidence_session",
        "evidence",
        ["session_id"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_session", table_name="evidence")
    op.drop_column("evidence", "session_id")
    op.drop_column("evidence", "payload")
