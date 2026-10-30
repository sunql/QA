"""KPI 指标技术形态：ontology_metric 扩列 + ontology_metric_source 新表 + kpi_catalog 扩列。

P1 指标技术形态治理（spec: docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md §5）。

- ontology_metric 新增 9 列：结构化轨道（numerator_measure / denominator_measure /
  aggregation_order / grain_keys / filter_expression / metric_type）+ 管理性
  （status / owner / execution_source）。
- ontology_metric_source 新表：多源类 M:N，role 必填。target_class_id 单值 FK
  保留不动（语义 = role=BASE 的类）。
- kpi_catalog 新增 3 列：grain_keys / numerator_columns / denominator_columns ——
  让治理侧能声明「我用的是哪几列」，机器比对从「中文 vs 结构」降级为「列集合比对」。

全部新列 nullable 或有默认值 ⇒ downgrade 直接删列，无数据损失；
ontology_metric_source 是新表 ⇒ 删表不影响存量。
UNIQUE(metric_id, class_id, role) 与 INDEX(class_id) 在 0137 里加（分开做，便于定位）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "ontology_metric",
        sa.Column("metric_type", sa.String(length=20), nullable=False, server_default="MEASURE"),
    )
    op.add_column("ontology_metric", sa.Column("numerator_measure", _JSON, nullable=True))
    op.add_column("ontology_metric", sa.Column("denominator_measure", _JSON, nullable=True))
    op.add_column("ontology_metric", sa.Column("aggregation_order", sa.String(length=24), nullable=True))
    op.add_column("ontology_metric", sa.Column("grain_keys", _JSON, nullable=True))
    op.add_column("ontology_metric", sa.Column("filter_expression", sa.Text(), nullable=True))
    op.add_column(
        "ontology_metric",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
    )
    op.add_column("ontology_metric", sa.Column("owner", sa.String(length=100), nullable=True))
    op.add_column("ontology_metric", sa.Column("execution_source", sa.String(length=16), nullable=True))

    op.add_column("kpi_catalog", sa.Column("grain_keys", _JSON, nullable=True))
    op.add_column("kpi_catalog", sa.Column("numerator_columns", _JSON, nullable=True))
    op.add_column("kpi_catalog", sa.Column("denominator_columns", _JSON, nullable=True))

    op.create_table(
        "ontology_metric_source",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "metric_id",
            sa.BigInteger(),
            sa.ForeignKey("ontology_metric.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "class_id",
            sa.BigInteger(),
            sa.ForeignKey("ontology_class.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ontology_metric_source")
    op.drop_column("kpi_catalog", "denominator_columns")
    op.drop_column("kpi_catalog", "numerator_columns")
    op.drop_column("kpi_catalog", "grain_keys")
    op.drop_column("ontology_metric", "execution_source")
    op.drop_column("ontology_metric", "owner")
    op.drop_column("ontology_metric", "status")
    op.drop_column("ontology_metric", "filter_expression")
    op.drop_column("ontology_metric", "grain_keys")
    op.drop_column("ontology_metric", "aggregation_order")
    op.drop_column("ontology_metric", "denominator_measure")
    op.drop_column("ontology_metric", "numerator_measure")
    op.drop_column("ontology_metric", "metric_type")
