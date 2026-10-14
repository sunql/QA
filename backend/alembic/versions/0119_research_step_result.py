"""research_step_result 表（研究步结果快照持久化）

触发：Task 6「ResearchStepResult / ResultSnapshot 持久化」（研究会话证据与平台计划）
变更：新建 research_step_result（列定义与 app/domain/research_models.py 的
      ResearchStepResult 一一对应）：

- ``run_state_id`` FK → ``research_run_state.id`` ON DELETE CASCADE（run state 随会话
  CASCADE；步结果随之删除）+ 该列索引（按 run state 读回）。
- ``step_index`` / ``step_type`` / ``sql_text`` / ``error`` / ``created_at``。
- ``raw_result_ref`` / ``evidence_summary`` 为 JSONB（原始行引用 / 证据摘要，可空）。

可空 / 有默认 ⇒ 对既有行安全；新表无存量行，无需 backfill。
幂等性：降级为 ``drop_index`` + ``drop_table``。
两库同步：需在 qa_metadata(prod) 与 qa_metadata_test 分别应用。

**手写迁移**（非 ``alembic revision --autogenerate``）：``alembic/env.py`` 读
``settings.databaseUrl``（``DATABASE_URL``），无视 ``TEST_DATABASE_URL`` ⇒ autogenerate
会连生产库；且两库存在结构漂移，autogenerate 的 diff 不可信。

Revision ID: 0119
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0119"
down_revision: str | None = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_step_result",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_state_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(50), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=True),
        sa.Column("raw_result_ref", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_summary", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_state_id"],
            ["research_run_state.id"],
            name=op.f("fk_research_step_result_run_state_id"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        op.f("ix_research_step_result_run_state_id"),
        "research_step_result",
        ["run_state_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_research_step_result_run_state_id"), table_name="research_step_result"
    )
    op.drop_table("research_step_result")
