"""dq rule auto-generation: ontology_property.allowed_values + rule 溯源三列

Revision ID: 0044_dq_rule_auto_generation
Revises: 0043_feature_rule_config
Why: spec §4 —— 字典值域沉淀 + 规则来源标记（幂等生成靠确定性 rule_code 去重）。
存量规则 derivation_type 由 server_default 回填 'MANUAL'。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0044_dq_rule_auto_generation"
down_revision = "0043_feature_rule_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ontology_property",
        sa.Column(
            "allowed_values",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column(
        "data_quality_rule",
        sa.Column("source_class_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "data_quality_rule",
        sa.Column("source_property_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "data_quality_rule",
        sa.Column(
            "derivation_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'MANUAL'"),
        ),
    )
    op.create_index(
        "ix_dq_rule_source_class", "data_quality_rule", ["source_class_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_dq_rule_source_class", table_name="data_quality_rule")
    op.drop_column("data_quality_rule", "derivation_type")
    op.drop_column("data_quality_rule", "source_property_id")
    op.drop_column("data_quality_rule", "source_class_id")
    op.drop_column("ontology_property", "allowed_values")
