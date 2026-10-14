"""add research_checkpoint.expires_at

Task B2: ResearchCheckpoint 加 expires_at 字段，nullable（向后兼容），
已有 checkpoint 行 expires_at = NULL（永不过期），新开的 checkpoint 由
_pauseForUser 在 openCheckpoint 时写入 ttl 截止时间。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0116"
down_revision: str | None = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_checkpoint",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_research_checkpoint_expires_at",
        "research_checkpoint",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_checkpoint_expires_at", table_name="research_checkpoint")
    op.drop_column("research_checkpoint", "expires_at")
