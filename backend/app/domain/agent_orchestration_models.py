"""ORM mappings for the isolated general Agent orchestration tables."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk, TimestampMixin


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentOrchestrationProfile(Base, TimestampMixin):
    __tablename__ = "agent_orchestration_profile"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    agent_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("agent_definition.id", ondelete="CASCADE"), nullable=False
    )
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    default_max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    default_max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    default_max_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(16, 8), nullable=False, default=Decimal("5"), server_default="5"
    )
    default_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900, server_default="900")
    max_observation_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=50000, server_default="50000")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("agent_id", name="uq_agent_orchestration_profile_agent"),
        CheckConstraint(
            "default_max_iterations > 0 AND default_max_tool_calls > 0 "
            "AND default_max_cost_usd >= 0 AND default_timeout_seconds > 0 "
            "AND max_observation_chars > 0",
            name="ck_agent_orchestration_profile_limits",
        ),
        CheckConstraint("revision >= 1", name="ck_agent_orchestration_profile_revision"),
    )


class AgentOrchestrationBinding(Base):
    __tablename__ = "agent_orchestration_binding"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("agent_orchestration_profile.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("profile_id", "tool_name", name="uq_agent_orchestration_binding_tool"),
        CheckConstraint("sort_order >= 0", name="ck_agent_orchestration_binding_sort_order"),
    )


class AgentOrchestrationRun(Base, TimestampMixin):
    __tablename__ = "agent_orchestration_run"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("agent_definition.id", ondelete="SET NULL"), nullable=True
    )
    agent_code: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    instructions_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    tool_bindings_snapshot: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    max_observation_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    model_config_id: Mapped[int] = mapped_column(BigIntFk, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED", server_default="QUEUED")
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(16, 8), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default="0"
    )
    cost_source: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    cost_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pause_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_agent_orchestration_run_run_id", "run_id", unique=True),
        Index("ix_agent_orchestration_run_status_id", "status", "id"),
        Index("ix_agent_orchestration_run_creator_time", "created_by", "created_time"),
        Index("ix_agent_orchestration_run_agent_time", "agent_id", "created_time"),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','PAUSED','SUCCEEDED','FAILED','CANCELLED')",
            name="ck_agent_orchestration_run_status",
        ),
        CheckConstraint(
            "pause_code IN ('NEEDS_INPUT','AMBIGUOUS','PERMISSION_DENIED',"
            "'BUDGET_EXHAUSTED','TOOL_FAILED','PLANNER_OUTPUT_INVALID',"
            "'AGENT_UNAVAILABLE','INFRASTRUCTURE_ERROR')",
            name="ck_agent_orchestration_run_pause_code",
        ),
        CheckConstraint(
            "cost_source IN ('MEASURED','ESTIMATED','UNKNOWN','MIXED')",
            name="ck_agent_orchestration_run_cost_source",
        ),
        CheckConstraint("profile_revision >= 1 AND revision >= 1", name="ck_agent_orchestration_run_revision"),
        CheckConstraint(
            "max_iterations > 0 AND max_tool_calls > 0 AND max_cost_usd >= 0 "
            "AND timeout_seconds > 0 AND max_observation_chars > 0",
            name="ck_agent_orchestration_run_limits",
        ),
        CheckConstraint(
            "iteration_count >= 0 AND tool_call_count >= 0 AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 AND total_tokens >= 0 AND cost >= 0",
            name="ck_agent_orchestration_run_counters",
        ),
    )


class AgentOrchestrationStep(Base):
    __tablename__ = "agent_orchestration_step"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("agent_orchestration_run.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING", server_default="RUNNING")
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default="0"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "step_no", name="uq_agent_orchestration_step_run_step"),
        Index("ix_agent_orchestration_step_run_step", "run_id", "step_no"),
        CheckConstraint("kind IN ('PLAN','TOOL')", name="ck_agent_orchestration_step_kind"),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED')",
            name="ck_agent_orchestration_step_status",
        ),
        CheckConstraint("action IN ('CALL_TOOL','FINISH','ASK_USER')", name="ck_agent_orchestration_step_action"),
        CheckConstraint(
            "step_no >= 0 AND iteration_no >= 0 AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 AND cost >= 0",
            name="ck_agent_orchestration_step_counters",
        ),
    )


class AgentOrchestrationUsage(Base):
    __tablename__ = "agent_orchestration_usage"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("agent_orchestration_run.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("agent_orchestration_step.id", ondelete="SET NULL"), nullable=True
    )
    caller_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_config_id: Mapped[int] = mapped_column(BigIntFk, nullable=False)
    model_name: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default="0"
    )
    cost_source: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    call_status: Mapped[str] = mapped_column(String(32), nullable=False)
    request_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint("caller_type IN ('PLANNER','TOOL')", name="ck_agent_orchestration_usage_caller"),
        CheckConstraint(
            "cost_source IN ('MEASURED','ESTIMATED','UNKNOWN')",
            name="ck_agent_orchestration_usage_cost_source",
        ),
        CheckConstraint(
            "call_status IN ('SUCCEEDED','OUTPUT_INVALID','FAILED')",
            name="ck_agent_orchestration_usage_call_status",
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 "
            "AND (cached_tokens IS NULL OR cached_tokens >= 0) AND cost >= 0",
            name="ck_agent_orchestration_usage_counters",
        ),
    )
