"""kpi_catalog - KPI 业务目录表（Phase 4.1）。

业务视角的 KPI 治理元数据（与 ontology_metric 并存）：
- ontology_metric：技术形态（formula/agg_function/target_class_id）→ NL2SQL
- kpi_catalog：业务视角（VERSION/OWNER/UNIT/GRAIN/NUMERATOR/DENOMINATOR）→ 治理

metric_id 可空 FK → ontology_metric（KPI 可先于 metric 存在）。不进 Neo4j/Milvus。

命名采用 `0023_kpi_catalog` 而非计划文档的 `0022_kpi_catalog`：
1. 0022 已被 feat-ontology-governance-fields 占用来加 object_type/object_owner；
2. alembic_version.version_num 是 VARCHAR(32)，长 revision 标识会触发
   StringDataRightTruncationError。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023_kpi_catalog"
down_revision: str | None = "0022_ontology_class_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kpi_catalog",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kpi_code", sa.String(length=50), nullable=False),
        sa.Column("kpi_name", sa.String(length=200), nullable=False),
        sa.Column("business_definition", sa.Text(), nullable=True),
        sa.Column("formula", sa.Text(), nullable=True),
        sa.Column("numerator", sa.Text(), nullable=True),
        sa.Column("denominator", sa.Text(), nullable=True),
        sa.Column("grain", sa.String(length=100), nullable=True),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("data_source", sa.Text(), nullable=True),
        sa.Column("owner", sa.String(length=100), nullable=True),
        sa.Column(
            "version", sa.String(length=20), nullable=False, server_default="v1.0"
        ),
        sa.Column(
            "revision_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="DRAFT"
        ),
        sa.Column("metric_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.String(length=50), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["metric_id"], ["ontology_metric.id"], name="fk_kpi_catalog_metric"
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'DEPRECATED')",
            name="ck_kpi_catalog_status",
        ),
    )
    # 唯一约束：kpi_code 全表唯一（业务身份码）
    op.create_index(
        "uq_kpi_catalog_code",
        "kpi_catalog",
        ["kpi_code"],
        unique=True,
    )
    # 查询索引：按 status 过滤 + 按 owner 维度筛
    op.create_index(
        "ix_kpi_catalog_status",
        "kpi_catalog",
        ["status"],
    )
    op.create_index(
        "ix_kpi_catalog_owner",
        "kpi_catalog",
        ["owner"],
    )


def downgrade() -> None:
    op.drop_index("ix_kpi_catalog_owner", table_name="kpi_catalog")
    op.drop_index("ix_kpi_catalog_status", table_name="kpi_catalog")
    op.drop_index("uq_kpi_catalog_code", table_name="kpi_catalog")
    op.drop_table("kpi_catalog")
