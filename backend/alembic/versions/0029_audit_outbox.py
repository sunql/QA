"""audit_outbox - 审计 Outbox 表（feat-audit-outbox，Phase 4.5 扩展）。

业务与审计解耦：业务事务只写 audit_outbox（同事务、原子），独立 worker 进程
消费 outbox 写 audit_log / *_history。审计失败不回滚业务。

- id：BIGSERIAL PK，即幂等键（worker 按 id 消费，audit_log.outbox_id 唯一）
- event_type：'kpi_created' / 'feature_updated' / ...（worker 据此路由到
  audit_log 与/或 history 表）
- entity_id 可空：DELETE 事件在 history 侧无行可写时仍保留实体 id 供审计
- attempts/last_error：重试计数与最近错误；attempts >= 5 停止重试（防毒丸）

回滚：drop table 即可，不影响 audit_log 数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029_audit_outbox"
down_revision: str | None = "0028_feature_definition_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("actor_departments", postgresql.JSONB(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_audit_outbox_attempts_nonneg"
        ),
    )
    # pending 扫描（worker 主查询）：partial index 只覆盖未处理行
    op.create_index(
        "ix_audit_outbox_pending",
        "audit_outbox",
        ["created_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    # 已处理行按 processed_at 查询（排查/审计对账）
    op.create_index(
        "ix_audit_outbox_processed",
        "audit_outbox",
        ["processed_at"],
        postgresql_where=sa.text("processed_at IS NOT NULL"),
    )
    # audit_log 幂等键：同一 outbox 行至多写一条 audit_log
    op.add_column(
        "audit_log",
        sa.Column("outbox_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_audit_log_outbox",
        "audit_log",
        ["outbox_id"],
        unique=True,
        postgresql_where=sa.text("outbox_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_outbox", table_name="audit_log")
    op.drop_column("audit_log", "outbox_id")
    op.drop_index("ix_audit_outbox_processed", table_name="audit_outbox")
    op.drop_index("ix_audit_outbox_pending", table_name="audit_outbox")
    op.drop_table("audit_outbox")
