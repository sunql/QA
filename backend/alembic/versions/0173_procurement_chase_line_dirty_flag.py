"""催货行加「欠交量异常」标记位（0173）。

用户 2026-11-27 要求：催货单上**所有订单行**都能催（含脏数据 / 部分到货），
但**欠交量异常的行必须带红色标识、要求重点核对** —— 那类值多半是源侧录入错误
（如 150 万），催出去会把错值带给供应商。

- `is_dirty`：开单时**冻结**的布尔标记（阈值口径与待到货 / 到货跟踪**共用**
  `procurement_query_utils.OPEN_QTY_SANITY_MAX`）；
- 纯加列 DDL，零数据回填；`downgrade` drop 该列（催货单数据本身仍保留）。

Revision ID: 0173；down_revision: 0172（迁移号串行占号，不留空号）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0173"
down_revision = "0172"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "procurement_chase_line",
        sa.Column(
            "is_dirty",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
            comment="欠交量异常（≥ OPEN_QTY_SANITY_MAX）⇒ 催货单上红色标识，要求重点核对",
        ),
    )


def downgrade() -> None:
    op.drop_column("procurement_chase_line", "is_dirty")
