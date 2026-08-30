"""data_quality_rule - 数据质量规则定义表（Phase 1.1）。

一张表覆盖 5 种 rule_type 维度（COMPLETENESS / VALIDITY / UNIQUENESS /
CONSISTENCY / REFERENTIAL），TIMELINESS 留 Phase 2 血缘模块再补。评估执行
（Phase 1.2）与评分（Phase 1.3）将通过外部 service 调用实现，本表仅承载
规则定义本身的 CRUD。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_data_quality_rule"
down_revision: str | None = "0016_ontology_join"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_quality_rule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_name", sa.String(length=100), nullable=False),
        sa.Column("rule_code", sa.String(length=100), nullable=False),
        sa.Column("target_table", sa.String(length=100), nullable=False),
        sa.Column("target_column", sa.String(length=100), nullable=True),
        sa.Column(
            "rule_type",
            sa.String(length=20),
            nullable=False,
            server_default="COMPLETENESS",
        ),
        sa.Column("rule_expression", sa.Text(), nullable=True),
        sa.Column(
            "threshold", sa.Numeric(precision=5, scale=2), nullable=False, server_default="95.00"
        ),
        sa.Column(
            "severity",
            sa.String(length=10),
            nullable=False,
            server_default="MEDIUM",
        ),
        sa.Column(
            "is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "version", sa.String(length=20), nullable=False, server_default="v1.0"
        ),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # 与模型 __table_args__ 一致，避免 autogenerate 漂移
        sa.UniqueConstraint("rule_code", name="uq_data_quality_rule_code"),
    )
    op.create_index(
        "ix_data_quality_rule_table", "data_quality_rule", ["target_table"]
    )
    op.create_index(
        "ix_data_quality_rule_type", "data_quality_rule", ["rule_type"]
    )
    op.create_index(
        "ix_data_quality_rule_enabled", "data_quality_rule", ["is_enabled"]
    )


def downgrade() -> None:
    op.drop_index("ix_data_quality_rule_enabled", table_name="data_quality_rule")
    op.drop_index("ix_data_quality_rule_type", table_name="data_quality_rule")
    op.drop_index("ix_data_quality_rule_table", table_name="data_quality_rule")
    op.drop_table("data_quality_rule")
