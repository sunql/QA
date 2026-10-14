"""research_event_outbox 表（Task 12：事件持久化 outbox + SSE 回放）

触发：Task 12「持久化 Event Outbox + SSE 断线回放」（研究会话证据与平台计划）
变更：新建 research_event_outbox（列定义与 app/domain/research_models.py 的
      ResearchEventOutbox 一一对应）：

- ``seq`` BIGSERIAL 主键：单调递增游标（**刻意不用 ``created_at``** —— 同毫秒并列会
  漏读/重读、DB 时钟回拨会乱序；理由见模型 docstring）。
- ``session_id`` UUID + 索引（回放按会话查 ``WHERE seq > cursor ORDER BY seq``）。
  **不加 FK**：模型刻意不带 ``run_state_id`` / 跨表耦合；会话删除留孤儿行属后续任务。
- ``event`` varchar(50) / ``payload`` JSONB（默认 ``'{}'``）/ ``created_at`` timestamptz。

可空 / 有默认 ⇒ 对既有行安全；新表无存量行，无需 backfill。
幂等性：降级为 ``drop_index`` + ``drop_table``。
两库同步：需在 qa_metadata(prod) 与 qa_metadata_test 分别应用。

**手写迁移**（非 ``alembic revision --autogenerate``）：``alembic/env.py`` 读
``settings.databaseUrl``（``DATABASE_URL``），无视 ``TEST_DATABASE_URL`` ⇒ autogenerate
会连生产库；且两库存在结构漂移，autogenerate 的 diff 不可信。

Revision ID: 0120
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0120"
down_revision: str | None = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_event_outbox",
        sa.Column("seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("seq"),
    )
    op.create_index(
        op.f("ix_research_event_outbox_session_id"),
        "research_event_outbox",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_research_event_outbox_session_id"), table_name="research_event_outbox"
    )
    op.drop_table("research_event_outbox")
