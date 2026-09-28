"""Phase 7c: 站内消息 (feat-dq-evaluation-report)

新增 1 张表：
- in_app_message：站内消息收件箱。一行 = 一条发给某 user_id 的通知。
  用于 scheduler 跑出报告后给收件人发「报告已生成」提醒；前端 MessageBell
  轮询 unread-count + 列表。

Revision ID: 0073_in_app_message
Filename length: 24 chars（≤32 字符 OK）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "0073_in_app_message"
down_revision: str | None = "0072_evaluation_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "in_app_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("recipient", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link_url", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_in_app_message_recipient",
        "in_app_message",
        ["recipient"],
    )
    op.create_index(
        "ix_in_app_message_unread",
        "in_app_message",
        ["recipient", "read_at"],
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_in_app_message_unread", table_name="in_app_message")
    op.drop_index("ix_in_app_message_recipient", table_name="in_app_message")
    op.drop_table("in_app_message")