"""research_session 加 active_run_id（会话级执行权互斥）

触发：Task 2「并发运行互斥与幂等键」（研究会话证据与平台计划）
变更：research_session 新增可空列 active_run_id，FK → research_run_state.id
      ON DELETE SET NULL（run state 随会话 CASCADE 删除时不该阻塞会话行）
      + 该列索引（支撑 FK 的 SET NULL 级联，避免删 run state 时全表扫）
可空 ⇒ 存量行无需回填；无数据迁移。
幂等性：add_column / create_index 前不判存在，重复执行会报错；本迁移只跑一次
两库同步：需在 qa_metadata(prod) 与 qa_metadata_test 分别应用
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0118"
down_revision: str | None = "b1ea6cb32588"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_session",
        sa.Column("active_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_research_session_active_run_id"),
        "research_session",
        "research_run_state",
        ["active_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_research_session_active_run_id"),
        "research_session",
        ["active_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_research_session_active_run_id"), table_name="research_session")
    op.drop_constraint(
        op.f("fk_research_session_active_run_id"), "research_session", type_="foreignkey"
    )
    op.drop_column("research_session", "active_run_id")
