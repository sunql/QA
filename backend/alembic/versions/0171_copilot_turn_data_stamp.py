"""同日复用键加入数据新鲜度戳（P4 收尾）。

`procurement_copilot_turn.data_stamp`：场景分支复用判定用的数据新鲜度戳
（槽位实体在 feature_value 的最新 computed_at ISO 串）。指标重算 ⇒ 戳变化
⇒ 相同问题文本不再命中旧 run（数据变了就重算；数据没变仍零 LLM 成本复用）。
NULL = 非场景分支（自由文本，行为不变）。零数据回填，downgrade 直接删列。

revision: 0171；down_revision: 0170。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Sequence

revision: str = "0171"
down_revision: str | None = "0170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "procurement_copilot_turn",
        sa.Column("data_stamp", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("procurement_copilot_turn", "data_stamp")
