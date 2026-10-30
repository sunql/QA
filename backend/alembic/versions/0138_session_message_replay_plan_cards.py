"""session_message：query_plan + steps_snapshot（历史回放计划卡）。

2026-10-31，feat/history-replay-plan-cards。此前多步/单步消息落库只有汇总文本
（多步）或 sql/chart（单步），历史回放（刷新/重开会话）时分析计划卡与查询
计划卡全部丢失。

- ``query_plan``：单步消息顶层 NL2SQL 计划（JSONB），回放消息级 QueryPlanCard；
- ``steps_snapshot``：回放快照（JSONB 数组），回放 MultiStepPlanCard（含每步
  SQL/状态/查询计划/汇总步），与 multi_step_run/step 表（续跑 SSOT）解耦。

两列均可空：存量行与未携带快照的路径（闲聊/错误兜底等 20+ 调用点）行为不变。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

_TABLE = "session_message"
_JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("query_plan", _JSONB, nullable=True))
    op.add_column(_TABLE, sa.Column("steps_snapshot", _JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "steps_snapshot")
    op.drop_column(_TABLE, "query_plan")
