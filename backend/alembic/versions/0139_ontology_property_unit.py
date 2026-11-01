"""ontology_property：unit 列（字段单位治理）。

2026-11-01，feat/chat-output-zh-labels。chat 输出的数值字段需要显示单位，
而白名单列里没有任何单位字段；KPI/特征表的 unit 覆盖面只到已登记指标。
给本体属性加可空 unit 作为**字段级**单位来源（解析优先级最高）。

可空：存量 3717 行与未登记单位的字段行为不变。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_TABLE = "ontology_property"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("unit", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "unit")
