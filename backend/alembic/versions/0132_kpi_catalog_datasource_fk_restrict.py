"""kpi_catalog.datasource_id FK 改 RESTRICT（F2 写入流收尾 — 2026-10-18 spec §4.5）。

原 0125 加 FK 时 ondelete=SET NULL（兼容历史 NULL 行）。
本 migration 改 RESTRICT：删 data_source 时若有 KPI 引用，DB 抛 FK violation。
应用层 datasource_service.delete 已先抛 409（per spec §4.3 + Task 4），RESTRICT 是兜底。

部署前自检（per spec §4.5）：
  SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL;
  -- 必须返回 0；否则本 migration 部署会因数据约束阻塞，需先 backfill

注：原 0125 创建的 FK 约束实际名为 ``fk_kpi_catalog_datasource``（见
``backend/alembic/versions/0125_kpi_catalog_datasource_fk.py:35``），非默认命名
``<table>_<col>_fkey``。本 migration 沿用原名，确保 drop + create 命中同一约束。
"""
from __future__ import annotations
from alembic import op


revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_kpi_catalog_datasource", "kpi_catalog", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_kpi_catalog_datasource",
        "kpi_catalog", "data_source",
        ["datasource_id"], ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_kpi_catalog_datasource", "kpi_catalog", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_kpi_catalog_datasource",
        "kpi_catalog", "data_source",
        ["datasource_id"], ["id"],
        ondelete="SET NULL",
    )