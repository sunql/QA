"""data_quality_rule 加 datasource_id 列（Phase 1.2）。

评估执行需要按 datasource_id 找业务库适配器，因此规则定义必须能定位到具体
数据源。考虑到 Phase 1.1 表已建（含老环境与测试 PG），本次以独立迁移方式追加
datasource_id + FK + 索引，避免回溯修改 0017 影响 Phase 1.1 测试基线。

datasource_id 非空且 ON DELETE RESTRICT（保护评估历史不被误删）；同时加
索引便于按 datasource 过滤；并补一列回填项让旧规则批量挂到 demo datasource，
方便迁移灰度。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_dq_datasource"
down_revision: str | None = "0017_data_quality_rule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧库可能已有空数据，新库没有 —— 统一加列（nullable=True 临时）后回填
    op.add_column(
        "data_quality_rule",
        sa.Column("datasource_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_data_quality_rule_datasource",
        "data_quality_rule",
        "data_source",
        ["datasource_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_data_quality_rule_datasource",
        "data_quality_rule",
        ["datasource_id"],
    )
    # 历史行：第一个 demo 数据源（无则跳过）。生产灰度时由 DBA 手工跑脚本分配。
    op.execute(
        """
        UPDATE data_quality_rule dq
        SET datasource_id = (
            SELECT id FROM data_source
            WHERE is_active = TRUE
            ORDER BY id ASC LIMIT 1
        )
        WHERE dq.datasource_id IS NULL
        AND EXISTS (SELECT 1 FROM data_source WHERE is_active = TRUE)
        """
    )
    # 回填完毕后将列改为 NOT NULL
    op.alter_column(
        "data_quality_rule",
        "datasource_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_index("ix_data_quality_rule_datasource", table_name="data_quality_rule")
    op.drop_constraint(
        "fk_data_quality_rule_datasource", "data_quality_rule", type_="foreignkey"
    )
    op.drop_column("data_quality_rule", "datasource_id")