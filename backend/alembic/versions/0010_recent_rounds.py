"""session_query_state_recent_rounds - 3-4 保留会话最近 N 轮查询状态快照。

为 session_query_state 增加 recent_rounds（JSON，生产 PG 落 JSONB）：保存"上一轮"
之前的若干轮 (question, sql) 快照，按新到旧排列，供跨多轮 REFINE/FOLLOW_UP 回溯。
last_* 仍为最近一轮，recent_rounds 为其前历史；列可空，历史数据无需 backfill。
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_recent_rounds"
down_revision = "0009_ontology_property_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_query_state",
        sa.Column(
            "recent_rounds",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_query_state", "recent_rounds")
