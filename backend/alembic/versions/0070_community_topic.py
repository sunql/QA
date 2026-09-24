"""knowledge_community.topic - Phase 5.5 SPARSE_COMMUNITY 主题建议落点。

列含义：人工（或 LLM 建议后人工确认）起的社区主题描述。不参与
Louvain 重算，是给前端社区侧栏一个「这个社区讲什么」的语义标签。

SPARSE_COMMUNITY gap 的本质是「社区粒度太散 → 用户看不懂」。
``name`` 字段是社区编号（``C001``/``C002``...）—— 那是机器标识，
不是给人看的描述。``topic`` 才是给人看的描述。

Revision ID: 0070
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0070_community_topic"
down_revision: str | None = "0069_token_usage_compile_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'knowledge_community'
                  AND column_name = 'topic'
                  AND table_schema = 'public'
            ) THEN
                ALTER TABLE knowledge_community
                ADD COLUMN topic VARCHAR(200);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_community DROP COLUMN IF EXISTS topic")
