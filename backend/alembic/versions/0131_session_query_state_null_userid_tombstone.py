"""SessionQueryState 把 NULL user_id 行墓碑化为 -1。

Revision ID: 0131
Revises: 0130
Create Date: 2026-10-16

（头部原先误写 `Revision ID: 0128 / Revises: 0127`，与文件里的
`revision = "0131" / down_revision = "0130"` 对不上——0128/0127 那两个号在
2026-10-17 的 renumber 里已经换给了别的迁移。此处只改正文档字符串，不动
`revision` / `down_revision` 常量：本迁移已在各环境 apply 过。）

M4：P0-3 上线前写的 `(NULL, NULL, session_id)` 旧行（无 user_id 归属）可被任何
用户读 —— 因为 P0-3 三元 (user, datasource, session) 过滤在 NULL 行不命中，
落到 session_id-only 路径被任意 user 命中，造成跨用户上下文污染。

修法（与 aichat-p2-followups-2026-10-16/remaining-work-ssot §M4 方案 A 对齐）：
- UPDATE 把所有 `user_id IS NULL` 的行置为 -1（墓碑值，表示「P0-3 前的遗留桶，
  归属不明，禁止读」）
- 与 models.py:881 的 `nullable=True` 兼容注释一致：NULL 仍允许（不破坏旧调用点），
  但读取侧统一 `WHERE user_id != -1` 排除墓碑
- 选 -1 而非加新列：少 1 个 schema 改动；BigInteger 范围充足；与已有 ON DELETE
  SET NULL 习惯一致（语义都是「被清除/失效」）
- 不加 CHECK 约束阻止新 NULL 行：旧 17+ 调用点仍可写 NULL（向后兼容）；
  新调用点应主动传 userId，NULL 仅是过渡期残留
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


# 墓碑值：约定所有 user_id = -1 的行表示「P0-3 前的 NULL 遗留桶，禁止读」。
# 与 models.py SessionQueryState.user_id (BigInteger) 兼容（int8 范围充足）。
_TOMBSTONE_USER_ID = -1


def upgrade() -> None:
    # 1. 墓碑化所有 P0-3 前的 NULL 行
    op.execute(
        sa.text(
            "UPDATE session_query_state SET user_id = :tombstone "
            "WHERE user_id IS NULL"
        ).bindparams(tombstone=_TOMBSTONE_USER_ID)
    )

    # 2. 不加 CHECK 约束 —— 旧 17+ 调用点可能继续写 NULL（向后兼容期）。
    #    读侧统一 `WHERE user_id != -1` 排除墓碑即可。
    #    注释加在表上方便未来 reviewer 看到约定。
    op.execute(
        sa.text(
            "COMMENT ON COLUMN session_query_state.user_id IS "
            "'P0-3 归属用户 ID；-1 = 墓碑（P0-3 前遗留 NULL 行），读取侧 WHERE user_id != -1 排除'"
        )
    )


def downgrade() -> None:
    # 还原墓碑行为 NULL（保留历史数据原貌）
    op.execute(
        sa.text(
            "UPDATE session_query_state SET user_id = NULL "
            "WHERE user_id = :tombstone"
        ).bindparams(tombstone=_TOMBSTONE_USER_ID)
    )
    op.execute(
        sa.text("COMMENT ON COLUMN session_query_state.user_id IS NULL")
    )
