"""wiki 核心 4 表 - Phase 8 M1。

新增「通用知识管理」的地基表：

- ``wiki_page``：知识条目本体（Markdown + 维度/结构阶段/自动分类元数据）
- ``knowledge_claim``：从 Page 抽取的事实原子
- ``evidence``：Claim 的证据出处（来源 5 元组）
- ``knowledge_relation``：Page → Page/Class/Metric/Entity 关系（含 confirmed 审核位）

建表用 ``IF NOT EXISTS`` 幂等守卫（与 0052 同思路）：pg_restore 恢复后再跑
alembic 不应爆。本轮不 seed 任何业务数据 —— wiki 是空库起步、由业务专家写入。

Revision ID: 0053
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0053_wiki_core_tables"
down_revision: str | None = "0052_system_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSON 列的跨库写法（与 app/domain/wiki_models.py 的 JsonColumn 一致）
_JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # ---- wiki_page：知识条目本体 ----
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_page (
            id                  BIGSERIAL PRIMARY KEY,
            page_id             VARCHAR(64)  NOT NULL,
            title               VARCHAR(200) NOT NULL,
            content             TEXT         NOT NULL,
            dimension           VARCHAR(30),
            structure_stage     VARCHAR(20)  NOT NULL DEFAULT 'MARKDOWN',
            auto_classification JSONB,
            status              VARCHAR(20)  NOT NULL DEFAULT 'DRAFT',
            authority_level     VARCHAR(10),
            version             VARCHAR(30)  NOT NULL DEFAULT 'v1.0',
            created_by_user_id  BIGINT,
            valid_from          TIMESTAMP WITH TIME ZONE,
            valid_to            TIMESTAMP WITH TIME ZONE,
            created_time        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            updated_time        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_wiki_page_page_id UNIQUE (page_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_page_dimension_status "
        "ON wiki_page (dimension, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_page_structure_stage "
        "ON wiki_page (structure_stage)"
    )

    # ---- knowledge_claim：事实原子 ----
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_claim (
            id            BIGSERIAL PRIMARY KEY,
            page_id       VARCHAR(64) NOT NULL,
            claim_text    TEXT        NOT NULL,
            claim_type    VARCHAR(30),
            embedding_ref VARCHAR(128),
            created_time  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT fk_knowledge_claim_page
                FOREIGN KEY (page_id) REFERENCES wiki_page (page_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_claim_page ON knowledge_claim (page_id)"
    )

    # ---- evidence：Claim 的出处 ----
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence (
            id           BIGSERIAL PRIMARY KEY,
            claim_id     BIGINT      NOT NULL,
            source_type  VARCHAR(30) NOT NULL,
            source_id    VARCHAR(128),
            page_number  VARCHAR(30),
            section_name VARCHAR(200),
            paragraph_no VARCHAR(30),
            content      TEXT,
            created_time TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT fk_evidence_claim
                FOREIGN KEY (claim_id) REFERENCES knowledge_claim (id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_evidence_claim ON evidence (claim_id)")

    # ---- knowledge_relation：知识关系（含审核位）----
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_relation (
            id               BIGSERIAL PRIMARY KEY,
            upstream_page_id VARCHAR(64)  NOT NULL,
            downstream_type  VARCHAR(30)  NOT NULL,
            downstream_id    VARCHAR(128) NOT NULL,
            relation_type    VARCHAR(50)  NOT NULL,
            confidence       NUMERIC(4, 3),
            auto_detected    BOOLEAN      NOT NULL DEFAULT FALSE,
            confirmed        BOOLEAN      NOT NULL DEFAULT FALSE,
            created_time     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_knowledge_relation_triple UNIQUE (
                upstream_page_id, downstream_type, downstream_id, relation_type
            ),
            CONSTRAINT fk_knowledge_relation_upstream
                FOREIGN KEY (upstream_page_id) REFERENCES wiki_page (page_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_relation_upstream "
        "ON knowledge_relation (upstream_page_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_relation_downstream "
        "ON knowledge_relation (downstream_type, downstream_id)"
    )


def downgrade() -> None:
    # 逆依赖顺序 drop（claims/relations 先于 page）
    op.execute("DROP TABLE IF EXISTS knowledge_relation")
    op.execute("DROP TABLE IF EXISTS evidence")
    op.execute("DROP TABLE IF EXISTS knowledge_claim")
    op.execute("DROP TABLE IF EXISTS wiki_page")
