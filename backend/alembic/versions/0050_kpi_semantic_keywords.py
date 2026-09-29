"""add_semantic_keywords_to_kpi_catalog - Phase 1 L1 语义匹配层 schema 准备。

扩展 kpi_catalog 表加两个字段供 L1 语义匹配使用：
- semantic_keywords text[]：同义词关键词数组（GIN 索引加速 array contains 查询）
- match_threshold numeric(3,2)：Jaccard 匹配阈值，默认 0.75

downgrade 正确移除列 + 索引（与 upgrade 顺序严格对称）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0050_kpi_semantic_kw"
down_revision: str | None = "0049_ontology_relation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. 加 semantic_keywords 列（ARRAY of varchar(64)，可空）
    op.add_column(
        "kpi_catalog",
        sa.Column("semantic_keywords", sa.ARRAY(sa.String(64)), nullable=True),
    )
    # 2. 加 match_threshold 列（numeric(3,2)，非空，默认 0.75）
    op.add_column(
        "kpi_catalog",
        sa.Column(
            "match_threshold",
            sa.Numeric(3, 2),
            nullable=False,
            server_default="0.75",
        ),
    )
    # 3. GIN 索引加速 semantic_keywords 的任意关键词查询
    op.create_index(
        "ix_kpi_catalog_semantic_keywords",
        "kpi_catalog",
        ["semantic_keywords"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    # downgrade 必须与 upgrade 严格对称：先删索引，再删列
    op.drop_index("ix_kpi_catalog_semantic_keywords", table_name="kpi_catalog")
    op.drop_column("kpi_catalog", "match_threshold")
    op.drop_column("kpi_catalog", "semantic_keywords")
