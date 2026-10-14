"""llm_config 加 disable_thinking 列：按模型勾选关闭推理模型的思维链。

背景（2026-10-03 真机）：MiniMax-M3 在计划阶段写 91.3% 的 token 在 <think>
思维链里（实测：think 5829 tokens / JSON 558 tokens），而计划阶段 maxTokens
上限是函数签名硬编码的 2048 ⇒ 回复被截断在 JSON 之前 ⇒ 连续 3 次
PLAN_REPLY_EMPTY ⇒ chat 报 HTTP 400「无法生成有效的查询计划」。

关闭 thinking 后实测同prompt 只需 509~907 tokens 且解析全部成功（省 86-92%）。
本列让 admin 按模型勾选（M3 支持关闭，M2.x 不支持——不支持的模型传了也无害，
语义由用户承担）。

server_default=false：既有配置行自动为「不关闭」，保持现状不变。

Revision ID: 0109
Revises: 0108
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0109"
down_revision: str | None = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_config",
        sa.Column(
            "disable_thinking",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_config", "disable_thinking")