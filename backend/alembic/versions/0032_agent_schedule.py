"""agent_schedule + agent_run_log（Phase 7 G5 feat-agent-scheduler）。

agent_schedule：cron 表达式 + 执行参数 + 下次执行时间（next_run_at）。
worker 进程轮询 ``is_active AND next_run_at <= now`` 取到期行，
条件 UPDATE 前移 next_run_at 完成 claim（防并发双跑）。

agent_run_log：每次调度的运行历史（status/answer/error/tokens/cost/actor），
schedule_id SET NULL 级联删除，保留审计追溯。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032_agent_schedule"
down_revision: str | None = "0031_agent_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # agent_schedule
    # -------------------------------------------------------------------------
    op.create_table(
        "agent_schedule",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "agent_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_definition.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cron_expression", sa.String(length=64), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # worker 主索引：(is_active, next_run_at) 让到期扫描走索引
    op.create_index(
        "ix_agent_schedule_next_run",
        "agent_schedule",
        ["is_active", "next_run_at"],
    )
    op.create_index("ix_agent_schedule_agent", "agent_schedule", ["agent_id"])

    # -------------------------------------------------------------------------
    # agent_run_log
    # -------------------------------------------------------------------------
    op.create_table(
        "agent_run_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "schedule_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_schedule.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("agent_code", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "tokens_used", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost",
            sa.Numeric(precision=14, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('success','error')",
            name="ck_agent_run_log_status",
        ),
    )
    op.create_index(
        "ix_agent_run_log_schedule", "agent_run_log", ["schedule_id"]
    )
    op.create_index(
        "ix_agent_run_log_agent",
        "agent_run_log",
        ["agent_code", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_log_agent", table_name="agent_run_log")
    op.drop_index("ix_agent_run_log_schedule", table_name="agent_run_log")
    op.drop_table("agent_run_log")
    op.drop_index("ix_agent_schedule_agent", table_name="agent_schedule")
    op.drop_index("ix_agent_schedule_next_run", table_name="agent_schedule")
    op.drop_table("agent_schedule")