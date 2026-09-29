"""session_query_state 加 inheritance_snapshot JSONB（v3.1 任务 B5 / 蓝图 §15.3）

Revision ID: 0101
Revises: 0100
Create Date: 2026-09-29

背景：多轮字段继承快照（metric / time / filters），由 _resolveInheritedState
单一入口写入，供下一轮继承链路读取。JSONB 与 last_plan 同变体。

downgrade = drop column（无数据语义，对称反转）。

生产部署注意（两库使用策略）：本迁移对 prod qa_metadata 执行前，
先备份 session_query_state_<YYYYMMDD>；本任务只对测试库
（qa-pg-a1 / qa_metadata_test）验证往返，prod 迁移由部署流程统一执行。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_query_state",
        sa.Column(
            "inheritance_snapshot",
            sa.JSON().with_variant(sa.JSON(), "postgresql"),
            nullable=True,
            comment="B5 字段继承快照：inherited_metric / inherited_time / inherited_filters / inheritance_confidence（v3.1 §15.3）",
        ),
    )


def downgrade() -> None:
    op.drop_column("session_query_state", "inheritance_snapshot")
