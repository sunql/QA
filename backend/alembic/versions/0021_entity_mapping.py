"""entity_mapping - 跨系统编码映射表（Phase 3.1）。

将各源系统（ERP/SRM/QMS/MDM/PLM）的原始编码映射到企业统一代理键
（enterprise_key）与统一编码（enterprise_code），满足采购域 §三 / Sheet 05。
唯一约束 (entity_type, enterprise_key, source_system) 保证同一实体在
同一源系统只有一条映射；service 层抛 ValidationError 兜底（DB 防 race）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_entity_mapping"
down_revision: str | None = "0020_data_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_mapping",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("enterprise_key", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_code", sa.String(length=100), nullable=False),
        sa.Column("source_system", sa.String(length=20), nullable=False),
        sa.Column("source_key", sa.String(length=100), nullable=False),
        sa.Column("source_code", sa.String(length=100), nullable=False),
        sa.Column(
            "match_rule",
            sa.String(length=20),
            nullable=False,
            server_default="MAPPING",
        ),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "entity_type IN ('SUPPLIER', 'MATERIAL', 'PO', 'GR', 'IQC', 'NCR')",
            name="ck_entity_mapping_entity_type",
        ),
        sa.CheckConstraint(
            "source_system IN ('ERP', 'SRM', 'QMS', 'MDM', 'PLM')",
            name="ck_entity_mapping_source_system",
        ),
        sa.CheckConstraint(
            "match_rule IN ('MDM_MASTER', 'BUSINESS_KEY', 'MAPPING')",
            name="ck_entity_mapping_match_rule",
        ),
    )
    # 唯一约束：同一实体 + 同一源系统只允许一条映射；
    # PG 唯一约束对 NULL 列不冲突（SQL 标准语义），此处三列均非空，DB 约束即完整兜底。
    op.create_index(
        "uq_entity_mapping_entity_source",
        "entity_mapping",
        ["entity_type", "enterprise_key", "source_system"],
        unique=True,
    )
    # 查询索引：企业代理键 / 源系统原始 key（跨系统对照查询入口）
    op.create_index(
        "ix_entity_mapping_enterprise_key",
        "entity_mapping",
        ["enterprise_key"],
    )
    op.create_index(
        "ix_entity_mapping_source",
        "entity_mapping",
        ["entity_type", "source_system", "source_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_entity_mapping_source", table_name="entity_mapping")
    op.drop_index("ix_entity_mapping_enterprise_key", table_name="entity_mapping")
    op.drop_index("uq_entity_mapping_entity_source", table_name="entity_mapping")
    op.drop_table("entity_mapping")
