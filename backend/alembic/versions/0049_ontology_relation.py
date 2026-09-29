"""ontology_relation - 本体「类 × 类」语义关系表（Phase 5.6 关系重构）。

一行 = 用户显式声明的 (source_class_id → target_class_id × relation_type)
方向性语义关系。与 ontology_join（按列配对的 NL2SQL JOIN 目录）分工：
本表无列、无 join_key，按 (source, target, relation_type) 三元组唯一去重。

PG 为 SSOT；Neo4j (:Class)-[:{relation_type}]->(:Class) 为镜像（失败不阻断 PG）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049_ontology_relation"
down_revision: str | None = "0048_schema_cache_schema_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ontology_relation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_class_id", sa.BigInteger(), nullable=False),
        sa.Column("target_class_id", sa.BigInteger(), nullable=False),
        sa.Column("relation_type", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_class_id", "target_class_id", "relation_type",
            name="uq_ontology_relation_triple",
        ),
        sa.ForeignKeyConstraint(
            ["source_class_id"], ["ontology_class.id"], name="fk_relation_source_class"
        ),
        sa.ForeignKeyConstraint(
            ["target_class_id"], ["ontology_class.id"], name="fk_relation_target_class"
        ),
    )
    op.create_index("idx_relation_source_class", "ontology_relation", ["source_class_id"])
    op.create_index("idx_relation_target_class", "ontology_relation", ["target_class_id"])


def downgrade() -> None:
    op.drop_index("idx_relation_target_class", table_name="ontology_relation")
    op.drop_index("idx_relation_source_class", table_name="ontology_relation")
    op.drop_table("ontology_relation")
