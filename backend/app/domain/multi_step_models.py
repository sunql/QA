"""多步问答落库模型（spec: docs/superpowers/specs/2026-10-05-multi-step-persist.md §3）。"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_PARTIALLY_FAILED = "partially_failed"

STEP_STATUS_PENDING = "pending"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_SUCCEEDED = "succeeded"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_COMPRESSED = "compressed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MultiStepRun(Base):
    __tablename__ = "multi_step_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # chat 会话 id 是自由字符串（前端 `chat-<uuid>` / `docqa-<uuid>`，见 chatStore.ts:79），
    # **不是** UUID，也**没有** research_session 行可指 —— 故用 String(64) 且无 FK
    # （与 session_message / session_query_state 的既有约定一致；0115 迁移）。
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=RUN_STATUS_RUNNING)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_step_idx: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compressed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resume_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_multi_step_run_session_updated", "session_id", "updated_at"),
        Index("ix_multi_step_run_status_updated", "status", "updated_at"),
    )


class MultiStepStep(Base):
    __tablename__ = "multi_step_step"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("multi_step_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STEP_STATUS_PENDING)
    sub_question: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    data_compressed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    chart_option: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_multi_step_step_run_index"),
        Index("ix_multi_step_step_status_updated", "status", "updated_at"),
    )
