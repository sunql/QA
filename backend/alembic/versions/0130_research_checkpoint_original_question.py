"""research_checkpoint 加 original_question 列（X3 防御）。

Revision ID: 0130
Revises: 0129
Create Date: 2026-10-16

X3（1012 cross-refs spec §P0-X3）：检查点恢复保留本轮研究问题。
原实现只把 turn_id 挂在 checkpoint 上，恢复时 join turn.content 取
originalQuestion，但 turn.content 可能在 checkpoint 创建后被修改 /
序列化丢失（m2.7 chat 透传 JSON 时偶发丢键） / 跨会话污染。
现在 checkpoint 自带 original_question 列，挂起时固化，恢复阶段优先
用本列。

幂等性：server_default="" 兜底（空字符串视为"未填"，恢复时若
originalQuestion 为空则回退 turn.content 老路径，行为不破坏既有数据）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_checkpoint",
        sa.Column(
            "original_question",
            sa.String(length=2000),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("research_checkpoint", "original_question")
