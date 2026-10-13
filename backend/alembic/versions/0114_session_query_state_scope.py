"""session_query_state 加 datasource_id / schema_name / user_id / datasource_version。

Revision ID: 0114
Revises: 0113
Create Date: 2026-10-13

P0-3：SessionQueryState 改 (user_id, datasource_id, session_id) 三元约束，
避免切换数据源后用错 state / 跨用户窃取上下文。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增列（全部 nullable 兼容历史）
    op.add_column("session_query_state", sa.Column("datasource_id", sa.BigInteger(), nullable=True))
    op.add_column("session_query_state", sa.Column("schema_name", sa.String(length=128), nullable=True))
    op.add_column("session_query_state", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.add_column("session_query_state", sa.Column("datasource_version", sa.Integer(), nullable=True))

    # 2. 删旧 unique constraint（session_id 唯一）
    op.drop_constraint("uq_session_query_state_session", "session_query_state", type_="unique")

    # 3. 新增三元 unique（NULL 不互冲，所以历史 NULL 行不会冲突）
    op.create_unique_constraint(
        "uq_session_query_state_scope",
        "session_query_state",
        ["user_id", "datasource_id", "session_id"],
    )

    # 4. 索引：用户维度快速查
    op.create_index(
        "ix_session_query_state_user_datasource",
        "session_query_state",
        ["user_id", "datasource_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_query_state_user_datasource", table_name="session_query_state")
    op.drop_constraint("uq_session_query_state_scope", "session_query_state", type_="unique")
    op.create_unique_constraint(
        "uq_session_query_state_session", "session_query_state", ["session_id"]
    )
    op.drop_column("session_query_state", "datasource_version")
    op.drop_column("session_query_state", "user_id")
    op.drop_column("session_query_state", "schema_name")
    op.drop_column("session_query_state", "datasource_id")
