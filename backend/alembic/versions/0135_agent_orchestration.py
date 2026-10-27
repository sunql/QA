"""Additive persistence for the isolated general Agent orchestration path."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None

_PROFILE_SCHEMA = (
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column(
        "agent_id",
        sa.BigInteger(),
        sa.ForeignKey("agent_definition.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("instructions", sa.Text(), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("default_max_iterations", sa.Integer(), nullable=False, server_default="20"),
    sa.Column("default_max_tool_calls", sa.Integer(), nullable=False, server_default="30"),
    sa.Column("default_max_cost_usd", sa.Numeric(16, 8), nullable=False, server_default="5"),
    sa.Column("default_timeout_seconds", sa.Integer(), nullable=False, server_default="900"),
    sa.Column("max_observation_chars", sa.Integer(), nullable=False, server_default="50000"),
    sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("agent_id", name="uq_agent_orchestration_profile_agent"),
    sa.CheckConstraint(
        "default_max_iterations > 0 AND default_max_tool_calls > 0 "
        "AND default_max_cost_usd >= 0 AND default_timeout_seconds > 0 "
        "AND max_observation_chars > 0",
        name="ck_agent_orchestration_profile_limits",
    ),
    sa.CheckConstraint("revision >= 1", name="ck_agent_orchestration_profile_revision"),
)

_BINDING_SCHEMA = (
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column(
        "profile_id",
        sa.BigInteger(),
        sa.ForeignKey("agent_orchestration_profile.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("tool_name", sa.String(length=64), nullable=False),
    sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("profile_id", "tool_name", name="uq_agent_orchestration_binding_tool"),
    sa.CheckConstraint("sort_order >= 0", name="ck_agent_orchestration_binding_sort_order"),
)

_RUN_SCHEMA = (
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column("run_id", sa.String(length=36), nullable=False),
    sa.Column(
        "agent_id",
        sa.BigInteger(),
        sa.ForeignKey("agent_definition.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("agent_code", sa.String(length=64), nullable=False),
    sa.Column("profile_revision", sa.Integer(), nullable=False),
    sa.Column("instructions_snapshot", sa.Text(), nullable=False),
    sa.Column(
        "tool_bindings_snapshot",
        postgresql.JSONB(),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column("max_observation_chars", sa.Integer(), nullable=False),
    sa.Column("question", sa.Text(), nullable=False),
    sa.Column("created_by", sa.String(length=64), nullable=False),
    sa.Column("model_config_id", sa.BigInteger(), nullable=False),
    sa.Column("status", sa.String(length=32), nullable=False, server_default="QUEUED"),
    sa.Column("max_iterations", sa.Integer(), nullable=False),
    sa.Column("max_tool_calls", sa.Integer(), nullable=False),
    sa.Column("max_cost_usd", sa.Numeric(16, 8), nullable=False),
    sa.Column("timeout_seconds", sa.Integer(), nullable=False),
    sa.Column("iteration_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("cost", sa.Numeric(20, 8), nullable=False, server_default="0"),
    sa.Column("cost_source", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
    sa.Column("cost_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("final_answer", sa.Text(), nullable=True),
    sa.Column("pause_code", sa.String(length=64), nullable=True),
    sa.Column("pause_detail", postgresql.JSONB(), nullable=True),
    sa.Column("error_code", sa.String(length=64), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("lease_owner", sa.String(length=128), nullable=True),
    sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("id"),
    sa.CheckConstraint(
        "status IN ('QUEUED','RUNNING','PAUSED','SUCCEEDED','FAILED','CANCELLED')",
        name="ck_agent_orchestration_run_status",
    ),
    sa.CheckConstraint(
        "pause_code IN ('NEEDS_INPUT','AMBIGUOUS','PERMISSION_DENIED',"
        "'BUDGET_EXHAUSTED','TOOL_FAILED','PLANNER_OUTPUT_INVALID',"
        "'AGENT_UNAVAILABLE','INFRASTRUCTURE_ERROR')",
        name="ck_agent_orchestration_run_pause_code",
    ),
    sa.CheckConstraint(
        "cost_source IN ('MEASURED','ESTIMATED','UNKNOWN')",
        name="ck_agent_orchestration_run_cost_source",
    ),
    sa.CheckConstraint(
        "profile_revision >= 1 AND revision >= 1",
        name="ck_agent_orchestration_run_revision",
    ),
    sa.CheckConstraint(
        "max_iterations > 0 AND max_tool_calls > 0 AND max_cost_usd >= 0 "
        "AND timeout_seconds > 0 AND max_observation_chars > 0",
        name="ck_agent_orchestration_run_limits",
    ),
    sa.CheckConstraint(
        "iteration_count >= 0 AND tool_call_count >= 0 AND prompt_tokens >= 0 "
        "AND completion_tokens >= 0 AND total_tokens >= 0 AND cost >= 0",
        name="ck_agent_orchestration_run_counters",
    ),
)

_STEP_SCHEMA = (
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column(
        "run_id",
        sa.BigInteger(),
        sa.ForeignKey("agent_orchestration_run.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("step_no", sa.Integer(), nullable=False),
    sa.Column("iteration_no", sa.Integer(), nullable=False),
    sa.Column("kind", sa.String(length=32), nullable=False),
    sa.Column("status", sa.String(length=32), nullable=False, server_default="RUNNING"),
    sa.Column("action", sa.String(length=32), nullable=True),
    sa.Column("plan_summary", sa.Text(), nullable=True),
    sa.Column("tool_name", sa.String(length=64), nullable=True),
    sa.Column("input_json", postgresql.JSONB(), nullable=True),
    sa.Column("output_json", postgresql.JSONB(), nullable=True),
    sa.Column("answer", sa.Text(), nullable=True),
    sa.Column("error_code", sa.String(length=64), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("cost", sa.Numeric(20, 8), nullable=False, server_default="0"),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("run_id", "step_no", name="uq_agent_orchestration_step_run_step"),
    sa.CheckConstraint("kind IN ('PLAN','TOOL')", name="ck_agent_orchestration_step_kind"),
    sa.CheckConstraint(
        "status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED')",
        name="ck_agent_orchestration_step_status",
    ),
    sa.CheckConstraint(
        "action IN ('CALL_TOOL','FINISH','ASK_USER')",
        name="ck_agent_orchestration_step_action",
    ),
    sa.CheckConstraint(
        "step_no >= 0 AND iteration_no >= 0 AND prompt_tokens >= 0 "
        "AND completion_tokens >= 0 AND cost >= 0",
        name="ck_agent_orchestration_step_counters",
    ),
)

_USAGE_SCHEMA = (
    sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column(
        "run_id",
        sa.BigInteger(),
        sa.ForeignKey("agent_orchestration_run.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "step_id",
        sa.BigInteger(),
        sa.ForeignKey("agent_orchestration_step.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("caller_type", sa.String(length=32), nullable=False),
    sa.Column("model_config_id", sa.BigInteger(), nullable=False),
    sa.Column("model_name", sa.String(length=50), nullable=False),
    sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("cached_tokens", sa.Integer(), nullable=True),
    sa.Column("cost", sa.Numeric(20, 8), nullable=False, server_default="0"),
    sa.Column("cost_source", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
    sa.Column("call_status", sa.String(length=32), nullable=False),
    sa.Column("request_time", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("id"),
    sa.CheckConstraint("caller_type IN ('PLANNER','TOOL')", name="ck_agent_orchestration_usage_caller"),
    sa.CheckConstraint(
        "cost_source IN ('MEASURED','ESTIMATED','UNKNOWN')",
        name="ck_agent_orchestration_usage_cost_source",
    ),
    sa.CheckConstraint(
        "call_status IN ('SUCCEEDED','OUTPUT_INVALID','FAILED')",
        name="ck_agent_orchestration_usage_call_status",
    ),
    sa.CheckConstraint(
        "prompt_tokens >= 0 AND completion_tokens >= 0 "
        "AND (cached_tokens IS NULL OR cached_tokens >= 0) AND cost >= 0",
        name="ck_agent_orchestration_usage_counters",
    ),
)


def upgrade() -> None:
    op.create_table("agent_orchestration_profile", *_PROFILE_SCHEMA)
    op.create_table("agent_orchestration_binding", *_BINDING_SCHEMA)
    op.create_table("agent_orchestration_run", *_RUN_SCHEMA)
    op.create_index("uq_agent_orchestration_run_run_id", "agent_orchestration_run", ["run_id"], unique=True)
    op.create_index("ix_agent_orchestration_run_status_id", "agent_orchestration_run", ["status", "id"])
    op.create_index(
        "ix_agent_orchestration_run_creator_time",
        "agent_orchestration_run",
        ["created_by", "created_time"],
    )
    op.create_index(
        "ix_agent_orchestration_run_agent_time",
        "agent_orchestration_run",
        ["agent_id", "created_time"],
    )
    op.create_table("agent_orchestration_step", *_STEP_SCHEMA)
    op.create_index("ix_agent_orchestration_step_run_step", "agent_orchestration_step", ["run_id", "step_no"])
    op.create_table("agent_orchestration_usage", *_USAGE_SCHEMA)


def downgrade() -> None:
    op.drop_table("agent_orchestration_usage")
    op.drop_index("ix_agent_orchestration_step_run_step", table_name="agent_orchestration_step")
    op.drop_table("agent_orchestration_step")
    op.drop_index("ix_agent_orchestration_run_agent_time", table_name="agent_orchestration_run")
    op.drop_index("ix_agent_orchestration_run_creator_time", table_name="agent_orchestration_run")
    op.drop_index("ix_agent_orchestration_run_status_id", table_name="agent_orchestration_run")
    op.drop_index("uq_agent_orchestration_run_run_id", table_name="agent_orchestration_run")
    op.drop_table("agent_orchestration_run")
    op.drop_table("agent_orchestration_binding")
    op.drop_table("agent_orchestration_profile")
