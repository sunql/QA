"""ontology_join - 本体关联关系目录（运行时 JOIN 生成的唯一真源）。

join 边以一对 (source_columns, target_columns) 描述，来源分两类：
- foreign_key：由种子从本体外键标志物化（物化时写对目标列，修正"源列名≠目标主键名"）
- business：curated 业务流转关系（跨单据，无外键标志）

join_key 为幂等去重键（列按配对顺序拼接），跨 PG/SQLite 均可比较。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_ontology_join"
down_revision: str | None = "0015_term_dictionary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ontology_join",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_class_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_columns",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("target_class_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "target_columns",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("join_type", sa.String(length=10), nullable=False, server_default="INNER"),
        sa.Column("relation_type", sa.String(length=20), nullable=False, server_default="business"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("join_key", sa.String(length=300), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 与模型 __table_args__ 一致，避免 autogenerate 漂移
        sa.UniqueConstraint("join_key", name="uq_ontology_join_key"),
        sa.ForeignKeyConstraint(
            ["source_class_id"], ["ontology_class.id"], name="fk_join_source_class"
        ),
        sa.ForeignKeyConstraint(
            ["target_class_id"], ["ontology_class.id"], name="fk_join_target_class"
        ),
    )
    op.create_index("idx_join_source_class", "ontology_join", ["source_class_id"])
    op.create_index("idx_join_target_class", "ontology_join", ["target_class_id"])


def downgrade() -> None:
    op.drop_index("idx_join_target_class", table_name="ontology_join")
    op.drop_index("idx_join_source_class", table_name="ontology_join")
    op.drop_table("ontology_join")
