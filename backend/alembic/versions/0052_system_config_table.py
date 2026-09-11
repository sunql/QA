"""Add system_config runtime KV table - Task 5.3.

chat_service._isL4AgentLoopEnabled 读 system_config 表判断 L4 是否启用（键
ENABLE_L4_AGENT_LOOP）。该表 2026-09-11 前由手工 DDL 创建（chat 端点必查），
但缺少 alembic migration 跟进：9/10 pg wipe 恢复时漏掉，导致 chat 500
（UndefinedTableError 污染 transaction → 后续查询 InFailedSQLTransactionError）。

Revision ID: 0052
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0052_system_config"
down_revision: str | None = "0051_routing_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 幂等建表：fresh DB / pg_restore 后再跑都不爆
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS system_config (
            key          VARCHAR(64) PRIMARY KEY,
            value        TEXT,
            description  TEXT,
            updated_time TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )

    # 默认 KV：L4 默认关闭（安全降级；显式 UPDATE 改 'true' 才启用）
    op.execute(
        """
        INSERT INTO system_config (key, value, description)
        VALUES (
            'ENABLE_L4_AGENT_LOOP',
            'false',
            'L4 LangGraph agent loop 启用开关；true=启用，false=走 L1-L3'
        )
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    # 仅在迁移链整体回滚时执行；保留一行业务数据的 DELETE 是预期的
    op.execute("DELETE FROM system_config WHERE key = 'ENABLE_L4_AGENT_LOOP'")
    op.execute("DROP TABLE IF EXISTS system_config")