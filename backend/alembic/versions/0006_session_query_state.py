"""Phase 5: session_query_state table (ReAct multi-turn)

Revision ID: 0006_session_query_state
Revises: 0005_schema_cache
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_session_query_state"
down_revision: str | None = "0005_schema_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_query_state",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("last_question", sa.Text(), nullable=True),
        # JSON 字段：生产 PG 落 JSONB，SQLite（开发/测试）落 TEXT
        sa.Column(
            "last_plan",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("last_sql", sa.Text(), nullable=True),
        sa.Column(
            "last_result_columns",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 与模型 __table_args__ 一致，避免 autogenerate 漂移
        sa.UniqueConstraint("session_id", name="uq_session_query_state_session"),
    )


def downgrade() -> None:
    op.drop_table("session_query_state")
