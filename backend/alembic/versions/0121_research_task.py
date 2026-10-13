"""research_task 表（Task 10+11：进程内持久任务台账 + 启动回收）

触发：Task 10+11「重塑为进程内持久队列」（研究会话证据与平台计划）——
原计划「版本化快照表 + 独立 worker 进程」不可照做（会切断 SSE 流、打崩测试契约、
与 research_run_state 职责重叠），用户裁定走「保留内联执行 + 只加持久台账 + 启动回收」。

变更：新建 ``research_task``（列定义与 ``app/domain/research_models.py`` 的
``ResearchTask`` 一一对应）：

- ``id`` UUID 主键。
- ``session_id`` UUID FK → ``research_session.id`` **ondelete=CASCADE** + 索引
  （会话删除连带清台账）。
- ``turn_id`` UUID FK → ``research_turn.id`` **ondelete=CASCADE**，**可空** + 索引
  （resume 类任务由 checkpoint 驱动，无关联 turn）。
- ``kind`` varchar(20)：``turn`` | ``resume``；``status`` varchar(20)：
  ``pending`` | ``leased`` | ``done`` | ``failed`` | ``orphaned``。
- ``attempt`` / ``max_attempts`` int 非空（默认 0 / 1）：**只记录**，本 Task 不实现重试。
- ``worker_id`` varchar(64) 可空；``lease_until`` timestamptz 可空 + 索引：
  **只记录**，本 Task 的 reaper 不按租约 gate（单进程下「启动即全孤儿」才对）。
- ``payload`` JSONB（默认 ``'{}'``）；``error`` text 可空；
  ``created_at`` / ``updated_at`` timestamptz 非空。
- **不建** ``last_phase`` 列（无消费方，不自动重跑 = 明确裁定，YAGNI）。

可空 / 有默认 ⇒ 对既有行安全；新表无存量行，无需 backfill。
幂等性：降级为逆序 ``drop_index`` + ``drop_table``。
两库同步：需在 qa_metadata(prod) 与 qa_metadata_test 分别应用。

**手写迁移**（非 ``alembic revision --autogenerate``）：``alembic/env.py`` 读
``settings.databaseUrl``（``DATABASE_URL``），无视 ``TEST_DATABASE_URL`` ⇒ autogenerate
会连生产库；且两库存在结构漂移，autogenerate 的 diff 不可信。

Revision ID: 0121
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0121"
down_revision: str | None = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_task",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["research_session.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["research_turn.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        op.f("ix_research_task_session_id"), "research_task", ["session_id"], unique=False
    )
    op.create_index(
        op.f("ix_research_task_turn_id"), "research_task", ["turn_id"], unique=False
    )
    op.create_index(
        op.f("ix_research_task_lease_until"), "research_task", ["lease_until"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_research_task_lease_until"), table_name="research_task")
    op.drop_index(op.f("ix_research_task_turn_id"), table_name="research_task")
    op.drop_index(op.f("ix_research_task_session_id"), table_name="research_task")
    op.drop_table("research_task")
