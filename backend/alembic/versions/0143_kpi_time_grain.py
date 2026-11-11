"""KPI 时间粒度：kpi_catalog.time_grain + ontology_metric.time_grain / time_column。

P3 D1（spec: docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md §3/§6）：
治理侧显式声明口径的时间粒度（DAY/WEEK/MONTH/QUARTER/YEAR），并解析到源类的日期列。

- kpi_catalog.time_grain：VARCHAR(20)，nullable
- ontology_metric.time_grain：VARCHAR(20)，nullable
- ontology_metric.time_column：VARCHAR(100)，nullable

纯加法 DDL，不触碰既有列；全部 nullable ⇒ downgrade 直接删列，无数据损失。
值域由 `app.domain.enums.TimeGrain` / `TIME_GRAIN_VALUES` 守卫（DB 侧不是 ENUM）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kpi_catalog",
        sa.Column(
            "time_grain",
            sa.String(length=20),
            nullable=True,
            comment="0143，P3 时间粒度",
        ),
    )
    op.add_column(
        "ontology_metric",
        sa.Column(
            "time_grain",
            sa.String(length=20),
            nullable=True,
            comment="0143，P3 时间粒度",
        ),
    )
    op.add_column(
        "ontology_metric",
        sa.Column(
            "time_column",
            sa.String(length=100),
            nullable=True,
            comment="0143，P3 时间粒度",
        ),
    )


def downgrade() -> None:
    op.drop_column("ontology_metric", "time_column")
    op.drop_column("ontology_metric", "time_grain")
    op.drop_column("kpi_catalog", "time_grain")
