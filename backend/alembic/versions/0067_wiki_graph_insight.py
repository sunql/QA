"""wiki_graph_insight - Graph Insights 拓扑扫描结果 + LLM 解读缓存（Phase 3）。

一行 = 一条拓扑信号 + 一次 LLM 解读。
key 字段因 insight kind 而异：意外连接 = 「A↔B」两端 page_id 拼接，
桥接节点 = page_id，知识缺口 = page_id 或 community_key。
``kind`` 与 ``key`` 共同做唯一索引，确保同一拓扑重算只覆盖、不堆行。

``model_id`` 外键 llm_config.id：解读由哪个模型生成；无解读为 NULL。
``ondelete=SET NULL`` 让模型被删除时保留解读历史。

Revision ID: 0067
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0067_wiki_graph_insight"
down_revision: str | None = "0066_knowledge_community"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_graph_insight (
            id                BIGSERIAL PRIMARY KEY,
            kind              VARCHAR(40)  NOT NULL,
            key               VARCHAR(200) NOT NULL,
            payload           JSONB        NOT NULL DEFAULT '{}'::jsonb,
            headline          VARCHAR(300) NOT NULL,
            explanation       TEXT         NOT NULL DEFAULT '',
            model_id          BIGINT,
            explanation_chars INTEGER      NOT NULL DEFAULT 0,
            created_time      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_time      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT fk_graph_insight_model
                FOREIGN KEY (model_id) REFERENCES llm_config (id) ON DELETE SET NULL,
            CONSTRAINT uq_graph_insight_kind_key UNIQUE (kind, key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_graph_insight_kind ON wiki_graph_insight (kind)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wiki_graph_insight")
