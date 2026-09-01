"""entity_mapping - 加 name 列 + 索引（Phase 6.x AutoComplete UX）。

变更：
- 新增列 name VARCHAR(200) NULL：供应商 / 物料的中文名（THBI 同步写入），
  AutoComplete 下拉直接展示，不用回 THBI 查。
- 新增索引 ix_entity_mapping_enterprise_code / ix_entity_mapping_source_code：
  去掉 2 字符门槛后 1 字符前缀查询走索引（前缀 LIKE 'X%' 可用 btree）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_entity_mapping_name_index"
down_revision: str | None = "0033_menu_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entity_mapping",
        sa.Column("name", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_entity_mapping_enterprise_code",
        "entity_mapping",
        ["entity_type", "enterprise_code"],
    )
    op.create_index(
        "ix_entity_mapping_source_code",
        "entity_mapping",
        ["entity_type", "source_system", "source_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_entity_mapping_source_code", table_name="entity_mapping")
    op.drop_index("ix_entity_mapping_enterprise_code", table_name="entity_mapping")
    op.drop_column("entity_mapping", "name")
