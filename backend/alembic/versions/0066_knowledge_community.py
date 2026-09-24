"""knowledge_community + knowledge_community_member - Louvain 社区检测结果（Phase 2）。

知识图谱分析（4-Signal 相关性 + Louvain 社区检测）的持久化层：
- ``knowledge_community``：一次重算一代社区（key/cohesion/top_pages）
- ``knowledge_community_member``：Page ↔ 社区成员关系

重算语义是全量替换（删旧写新、同事务），不做增量 —— Louvain 的社区
编号跨次运行本就不稳定，增量对齐是伪需求。

Revision ID: 0066
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0066_knowledge_community"
down_revision: str | None = "0065_claim_evidence_column_fix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_community (
            id             BIGSERIAL PRIMARY KEY,
            community_key  VARCHAR(64)  NOT NULL,
            name           VARCHAR(200) NOT NULL,
            cohesion_score NUMERIC(4, 3) NOT NULL,
            page_count     INTEGER      NOT NULL,
            top_pages      JSONB        NOT NULL DEFAULT '[]'::jsonb,
            created_time   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_community_member (
            id           BIGSERIAL PRIMARY KEY,
            community_id BIGINT      NOT NULL,
            page_id      VARCHAR(64) NOT NULL,
            CONSTRAINT fk_community_member_community
                FOREIGN KEY (community_id) REFERENCES knowledge_community (id) ON DELETE CASCADE,
            CONSTRAINT fk_community_member_page
                FOREIGN KEY (page_id) REFERENCES wiki_page (page_id) ON DELETE CASCADE,
            CONSTRAINT uq_community_member UNIQUE (community_id, page_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_community_member_page "
        "ON knowledge_community_member (page_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_community_member")
    op.execute("DROP TABLE IF EXISTS knowledge_community")
