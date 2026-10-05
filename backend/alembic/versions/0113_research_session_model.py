"""research_session.model_id：研究会话级 LLM 模型选择（feat-research-entry-ux-fixes W5）

为什么是**列**而不是请求级参数：研究是**多轮可恢复**的 —— 会话 state 不落库，每个 turn
从 research_session 行 + 最新 checkpoint 重建。模型选择若只随创建请求传一次，追问与
resume 后就失效（用户会觉得「选了没用」）。与 datasource_id（0112）同一条理由、同一风格。

无 FK 约束：与 datasource_id 一致 —— 模型配置可被删除 / 停用，但历史会话应保留「当时用
的是哪个模型」这一事实。执行期读到失效模型时**显式报错**，不静默回落（见 W5 设计）。

Revision ID: 0113
Revises: 0112
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0113"
down_revision: str | None = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_session", sa.Column("model_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_session", "model_id")
