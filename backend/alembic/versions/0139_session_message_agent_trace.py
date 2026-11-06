"""session_message：agent_trace（L4 执行轨迹卡）。

2026-10-31，feat/l4-agent-trace。L4 Agent Loop 此前只落 answer + final_sql，
工具调用轨迹（list_tables → describe_table → execute_sql）在 loop 内部就丢了，
刷新/重开会话后无法解释「为什么查了这些表、执行了几次 SQL」。

``agent_trace``：JSONB 快照（l4_trace.buildTrace 构造），只含工具名 / 参数摘要 /
返回计数 / 耗时 / 成败，**不含 rows 或工具结果原文**。可空：存量行与所有非 L4
路径（L1/L2/L3、闲聊、降级）行为不变。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_TABLE = "session_message"
_JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("agent_trace", _JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "agent_trace")
