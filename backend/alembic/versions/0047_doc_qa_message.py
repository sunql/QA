"""doc_qa session_message 扩列 + 索引（Alembic 0047）。

新增 channel VARCHAR(16) NOT NULL DEFAULT 'chat'（chat/doc_qa 隔离）；
新增 citations JSONB NULL（doc_qa 轮填引用）；
新增 user_id VARCHAR(64) NULL（doc_qa ownership 守卫 + chat 未来扩展）；
新增 idx_session_msg_channel_time + idx_session_msg_user_channel_time 两个复合索引。
存量零破坏：channel 带 server_default='chat'，citations/user_id 为 nullable。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_doc_qa_message"
down_revision = "0046_org_sort_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_message",
        sa.Column(
            "channel",
            sa.String(16),
            nullable=False,
            server_default="chat",
        ),
    )
    op.add_column(
        "session_message",
        sa.Column("citations", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "session_message",
        sa.Column("user_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "idx_session_msg_channel_time",
        "session_message",
        ["channel", "session_id", "created_time"],
    )
    op.create_index(
        "idx_session_msg_user_channel_time",
        "session_message",
        ["user_id", "channel", "created_time"],
    )


def downgrade() -> None:
    op.drop_index("idx_session_msg_user_channel_time", table_name="session_message")
    op.drop_index("idx_session_msg_channel_time", table_name="session_message")
    op.drop_column("session_message", "user_id")
    op.drop_column("session_message", "citations")
    op.drop_column("session_message", "channel")