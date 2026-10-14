"""audit_log 加 entity_id_str 列 + entity_id 改 NULL

Phase X hotfix：research_checkpoint.id 是 UUID，塞不进 entity_id：BIGINT（asyncpg
报「value out of int64 range」）。已有 audit_service 调用方都用数字主键
（user.id / kpi.id / schedule.id），不动它们；checkpoint 走新列 entity_id_str
存 UUID 字符串。entity_id 同时改 NULL：避免为 UUID 实体留 0 当 sentinel。

不重构既有数据：现有行 entity_id 保留原值（int），entity_id_str = NULL；新写
checkpoint 决策行 entity_id = NULL，entity_id_str = UUID 字符串。两列并存由
应用层按 entity_type 路由：业务 PK 是 int → entity_id；是 UUID → entity_id_str。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0117"
down_revision: str | None = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_log",
        sa.Column("entity_id_str", sa.String(length=64), nullable=True),
    )
    # entity_id 不再强 NOT NULL：UUID 实体的行只填 entity_id_str。
    # 现有行 entity_id 仍是整数（语义未变），只是约束放宽。
    op.alter_column("audit_log", "entity_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.alter_column("audit_log", "entity_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("audit_log", "entity_id_str")