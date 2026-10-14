"""analysis_hypothesis 加 verification_status / support_count / rows_sample。

Revision ID: 0127
Revises: 0126
Create Date: 2026-10-14

P1-5：假设自动验证 + 证据回放。
verification_status 只存储用户显式点「验证」按钮后的结果，不自动执行。
renumbered 0116→0127 to align with main alembic head.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_hypothesis",
        sa.Column("verification_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "analysis_hypothesis",
        sa.Column("support_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "analysis_hypothesis",
        sa.Column("rows_sample", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analysis_hypothesis", "rows_sample")
    op.drop_column("analysis_hypothesis", "support_count")
    op.drop_column("analysis_hypothesis", "verification_status")
