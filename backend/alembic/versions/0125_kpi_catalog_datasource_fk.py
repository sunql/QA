"""kpi_catalog 加 datasource_id FK（feat-aichat-gap-f2-kpi-datasource）。

Revision ID: 0125
Revises: 0124
Create Date: 2026-10-14

Why: L1 KPI 命中时要做「KPI 绑定的数据源 vs 用户当前选择的数据源」一致性校验。
旧代码拿 KpiCatalog.data_source（text 业务源名）去比 dto.datasourceId（int 主键），
结构上永不成立（text != int）；且 kpi_catalog 表根本没有 datasource_id 列，
导致这段校验恒不触发，是死代码。

加真列 datasource_id（FK -> data_source.id, ON DELETE SET NULL），
让这个校验真正成立。SET NULL 而非 RESTRICT 的理由：
删除数据源是硬删除（datasource_service.py:205），无 FK 清理逻辑，
若用默认 RESTRICT 会把「删除有 KPI 绑定的数据源」这个可用功能打挂。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kpi_catalog",
        sa.Column("datasource_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_kpi_catalog_datasource",
        "kpi_catalog",
        "data_source",
        ["datasource_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_kpi_catalog_datasource", "kpi_catalog", type_="foreignkey")
    op.drop_column("kpi_catalog", "datasource_id")
