"""entity_mapping.owner 列（Phase 4.5 扩展：3 张表 owner-based ACL）。

Phase 4.5 仅在 kpi_catalog 应用了 owner-based ACL（依赖其 owner 列已存在）。
本迁移把 entity_mapping 补上 owner 列，让 entity_mapping_service.updateMapping /
deleteMapping 也能走 AclService.assertCanModify。

data_quality_rule.owner 与 ontology_class.object_owner 已存在（Phase 1.1 / 3.4），
不在本迁移范围。

设计：
- VARCHAR(100) nullable：与 kpi_catalog.owner / data_quality_rule.owner 同长度
- 索引 ix_entity_mapping_owner：按部门过滤是治理后台高频查询
- 旧数据 owner 留 NULL（service 层视 owner 为空时仅 admin 可改，符合预期）
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_entity_mapping_owner"
down_revision = "0025_audit_immutability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entity_mapping",
        sa.Column("owner", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_entity_mapping_owner",
        "entity_mapping",
        ["owner"],
    )


def downgrade() -> None:
    op.drop_index("ix_entity_mapping_owner", table_name="entity_mapping")
    op.drop_column("entity_mapping", "owner")