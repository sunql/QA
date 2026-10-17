"""session_query_state 加 is_stub 标记列。

Revision ID: 0129
Revises: 0127
Create Date: 2026-10-17

M5：新增 is_stub 列，标记 stub 模式（dbUserId=None）写入的半 scope 行。
- 写侧在 userId is None 时设 is_stub=True
- 读侧追加 WHERE is_stub=false 过滤
- _stateRowRank 将 is_stub=False 作为第一排序键（False < True，max 选 real 行）

Backfill：将 user_id=-1 的 M4 墓碑行标记为 is_stub=True（这些行是 stub 行，
在 M4 引入 user_id=-1 作为"无用户"的标记，M5 统一迁移到 is_stub 机制）。
Idempotent：backfill 的 WHERE user_id=-1 AND is_stub=False 只命中未处理行，
重复执行是安全的。

合并顺序：M4 (PR #35, branch fix/m4-session-query-state-null-stamp) 占 0128，
本迁移编 0129 是有意避开冲突的占位。
- 若 M4 先合：M5 rebase 后 down_revision 由 0127 改为 0128
- 若 M5 先合：M4 须把 0128 改号到 0130（按 qa-system-alembic-merge-number-collision 规约）
M5 自身不强制 depends_on（0128 在合并前不存在），靠 PR 协调 + SSOT 记录。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0129"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增 is_stub 列（NOT NULL + server_default=false 兼容历史行）
    op.add_column(
        "session_query_state",
        sa.Column("is_stub", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # 2. Backfill：M4 的 user_id=-1 墓碑行是 stub 行，标记为 is_stub=True
    op.execute(
        "UPDATE session_query_state SET is_stub = true "
        "WHERE user_id = -1 AND is_stub = false"
    )


def downgrade() -> None:
    op.drop_column("session_query_state", "is_stub")
