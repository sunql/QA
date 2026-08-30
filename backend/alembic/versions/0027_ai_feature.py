"""ai_feature - AI Feature Layer 双表（Phase 4.3）。

feature_definition（定义，一行一特征）+ feature_value（值，一行 = 特征 × 实体 × 有效期）。
把「LLM 每次实时聚合计算」转为「预计算特征、可复用、可追溯」。

设计偏离（见 Harness/changes/feat-ai-feature-model/summary.md §2）：
1. entity_key 用 VARCHAR(100) 业务编码（非 BIGINT 代理键）——实际业务键是字符串
   （供应商编码 Q630/B019、物料编码 RM-STEEL-001）；
2. value 用 DECIMAL(38,10) + value_text VARCHAR(500) 逃生口，不引 JSONB。

feature_value 级联删除：删 feature_definition 时一并删其值（ondelete CASCADE）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027_ai_feature"
down_revision: str | None = "0026_entity_mapping_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_definition",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feature_name", sa.String(length=100), nullable=False),
        sa.Column("feature_alias", sa.String(length=200), nullable=True),
        sa.Column("feature_definition", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("calculation_logic", sa.Text(), nullable=False),
        sa.Column("window_size", sa.String(length=20), nullable=True),
        sa.Column(
            "refresh_frequency", sa.String(length=20), nullable=False, server_default="DAILY"
        ),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column(
            "version", sa.String(length=20), nullable=False, server_default="v1.0"
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="DRAFT"
        ),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(length=50), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["datasource_id"], ["data_source.id"], name="fk_feature_definition_datasource"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'DEPRECATED')",
            name="ck_feature_definition_status",
        ),
    )
    op.create_index(
        "uq_feature_definition_name",
        "feature_definition",
        ["feature_name"],
        unique=True,
    )
    op.create_index(
        "ix_feature_definition_status", "feature_definition", ["status"]
    )
    op.create_index(
        "ix_feature_definition_owner", "feature_definition", ["owner"]
    )
    op.create_index(
        "ix_feature_definition_datasource", "feature_definition", ["datasource_id"]
    )

    op.create_table(
        "feature_value",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feature_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Numeric(precision=38, scale=10), nullable=True),
        sa.Column("value_text", sa.String(length=500), nullable=True),
        sa.Column("valid_at", sa.Date(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["feature_id"],
            ["feature_definition.id"],
            name="fk_feature_value_feature",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "value IS NOT NULL OR value_text IS NOT NULL",
            name="ck_feature_value_not_both_null",
        ),
    )
    op.create_index(
        "ix_feature_value_feature_id", "feature_value", ["feature_id"]
    )
    op.create_index(
        "uq_feature_value_dedup",
        "feature_value",
        ["feature_id", "entity_key", "valid_at"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_feature_value_dedup", table_name="feature_value")
    op.drop_index("ix_feature_value_feature_id", table_name="feature_value")
    op.drop_table("feature_value")
    op.drop_index("ix_feature_definition_datasource", table_name="feature_definition")
    op.drop_index("ix_feature_definition_owner", table_name="feature_definition")
    op.drop_index("ix_feature_definition_status", table_name="feature_definition")
    op.drop_index("uq_feature_definition_name", table_name="feature_definition")
    op.drop_table("feature_definition")
