"""data_lineage - 数据血缘边表（Phase 2.1）。

覆盖 7 层模型：SOURCE_SYSTEM / ODS / DWD / DWS / ADS / KPI / AI；
表级血缘 source/target_field 留空；字段级血缘必填。
唯一约束保护同上下游 + 字段组合不重复；service 层抛 ValidationError 兜底。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_data_lineage"
down_revision: str | None = "0019_dq_score"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_lineage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_layer", sa.String(length=20), nullable=False),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("source_object", sa.String(length=100), nullable=False),
        sa.Column("source_field", sa.String(length=100), nullable=True),
        sa.Column("target_layer", sa.String(length=20), nullable=False),
        sa.Column("target_system", sa.String(length=100), nullable=False),
        sa.Column("target_object", sa.String(length=100), nullable=False),
        sa.Column("target_field", sa.String(length=100), nullable=True),
        sa.Column("transformation_rule", sa.Text(), nullable=True),
        sa.Column(
            "refresh_frequency",
            sa.String(length=20),
            nullable=False,
            server_default="DAILY",
        ),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "source_layer IN ('SOURCE_SYSTEM', 'ODS', 'DWD', 'DWS', 'ADS', 'KPI', 'AI')",
            name="ck_data_lineage_source_layer",
        ),
        sa.CheckConstraint(
            "target_layer IN ('SOURCE_SYSTEM', 'ODS', 'DWD', 'DWS', 'ADS', 'KPI', 'AI')",
            name="ck_data_lineage_target_layer",
        ),
        sa.CheckConstraint(
            "refresh_frequency IN ('REALTIME', 'HOURLY', 'DAILY', 'WEEKLY')",
            name="ck_data_lineage_refresh_frequency",
        ),
    )
    # 唯一约束：同上下游 + 字段组合不允许重复；
    # PG 唯一约束在 NULL 列上不冲突（SQL 标准语义），由 service 层主动查重兜底。
    op.create_index(
        "uq_data_lineage_edge",
        "data_lineage",
        [
            "source_layer",
            "source_system",
            "source_object",
            "source_field",
            "target_layer",
            "target_system",
            "target_object",
            "target_field",
        ],
        unique=True,
    )
    # 列表查询索引：上游对象 / 下游对象 / 启用状态
    op.create_index(
        "ix_data_lineage_source",
        "data_lineage",
        ["source_layer", "source_system", "source_object"],
    )
    op.create_index(
        "ix_data_lineage_target",
        "data_lineage",
        ["target_layer", "target_system", "target_object"],
    )
    op.create_index("ix_data_lineage_active", "data_lineage", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_data_lineage_active", table_name="data_lineage")
    op.drop_index("ix_data_lineage_target", table_name="data_lineage")
    op.drop_index("ix_data_lineage_source", table_name="data_lineage")
    op.drop_index("uq_data_lineage_edge", table_name="data_lineage")
    op.drop_table("data_lineage")