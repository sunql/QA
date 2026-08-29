"""Phase 5: schema_cache table

Revision ID: 0005_schema_cache
Revises: 0004_session_message
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_schema_cache"
down_revision: str | None = "0004_session_message"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schema_cache",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        # schema_data：生产 PG 落 JSONB，SQLite（开发/测试）落 TEXT
        sa.Column(
            "schema_data",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["datasource_id"], ["data_source.id"]),
        sa.PrimaryKeyConstraint("id"),
        # 与模型 __table_args__ 一致（UniqueConstraint），避免 autogenerate 漂移
        sa.UniqueConstraint("datasource_id", name="uq_schema_cache_datasource"),
    )


def downgrade() -> None:
    op.drop_table("schema_cache")
