"""多步问答落库：multi_step_run + multi_step_step

触发：spec docs/superpowers/specs/2026-10-05-multi-step-persist.md §3/§10.1
变更：新建两张表（UUID PK，FK CASCADE 到 research_session / multi_step_run）
幂等性：op.create_table 前不判存在，重复执行会报错；本迁移只跑一次
两库同步：需在 qa_metadata(prod) 与 qa_metadata_test 分别应用
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0114"
down_revision: str | None = "0113"
branch_labels = None
depends_on = None

_JSONB_EMPTY_LIST = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.create_table(
        "multi_step_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("datasource_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("total_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_step_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compressed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_keys", postgresql.JSONB(), nullable=False, server_default=_JSONB_EMPTY_LIST),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_multi_step_run_session_id", "multi_step_run", ["session_id"])
    op.create_index("ix_multi_step_run_session_updated", "multi_step_run", ["session_id", "updated_at"])
    op.create_index("ix_multi_step_run_status_updated", "multi_step_run", ["status", "updated_at"])

    op.create_table(
        "multi_step_step",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("multi_step_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("sub_question", sa.Text(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("sql_hash", sa.String(64), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("data_compressed", postgresql.JSONB(), nullable=True),
        sa.Column("chart_option", postgresql.JSONB(), nullable=True),
        sa.Column("model_used", sa.String(64), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_kind", sa.String(20), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "step_index", name="uq_multi_step_step_run_index"),
    )
    op.create_index("ix_multi_step_step_run_id", "multi_step_step", ["run_id"])
    op.create_index("ix_multi_step_step_status_updated", "multi_step_step", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_multi_step_step_status_updated", table_name="multi_step_step")
    op.drop_index("ix_multi_step_step_run_id", table_name="multi_step_step")
    op.drop_table("multi_step_step")
    op.drop_index("ix_multi_step_run_status_updated", table_name="multi_step_run")
    op.drop_index("ix_multi_step_run_session_updated", table_name="multi_step_run")
    op.drop_index("ix_multi_step_run_session_id", table_name="multi_step_run")
    op.drop_table("multi_step_run")
