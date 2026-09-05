"""add feature_rule + feature_rule_threshold tables

Revision ID: 0043_feature_rule_config
Revises: 0042_doc_rel_key_varchar
Create Date: 2026-09-05

Why: feat-feature-rule-config 把硬编码 RISK_RULES + DEFAULT_SUPPLIER_FEATURES
迁到 DB SSOT（spec §5）。两张表头档 + 阈值档（1:N）。unique 约束保
(data_object, data_layer, target_level, code) 唯一。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0043_feature_rule_config"
down_revision = "0042_doc_rel_key_varchar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_rule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("data_object", sa.String(length=64), nullable=False),
        sa.Column("data_layer", sa.String(length=16), nullable=False),
        sa.Column("target_level", sa.String(length=16), nullable=False),
        sa.Column("feature_name", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("policy_description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "data_object",
            "data_layer",
            "target_level",
            "code",
            name="uq_feature_rule_scope_code",
        ),
    )
    op.create_index(
        "ix_feature_rule_scope_enabled",
        "feature_rule",
        ["data_object", "data_layer", "target_level", "enabled"],
    )

    op.create_table(
        "feature_rule_threshold",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=16), nullable=False),
        sa.Column("threshold_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column(
            "threshold_order", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["feature_rule.id"],
            ondelete="CASCADE",
            name="fk_feature_rule_threshold_rule",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "rule_id", "severity", name="uq_feature_rule_threshold_rule_severity"
        ),
    )


def downgrade() -> None:
    op.drop_table("feature_rule_threshold")
    op.drop_index("ix_feature_rule_scope_enabled", table_name="feature_rule")
    op.drop_table("feature_rule")
