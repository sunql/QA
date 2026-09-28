"""P3 批量知识编译器的领域词表与 ORM。"""
from __future__ import annotations
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.domain.models import Base, BigIntFk, BigIntPk

# ===== Task 1 constants =====
COMPILE_SCOPES: tuple[str, ...] = ("ALL", "PAGE_IDS", "DIMENSION")
TASK_STATUS_PENDING = "PENDING"
TASK_STATUS_RUNNING = "RUNNING"
TASK_STATUS_SUCCEEDED = "SUCCEEDED"
TASK_STATUS_PARTIAL = "PARTIAL"
TASK_STATUS_FAILED = "FAILED"
COMPILE_TASK_STATUSES: tuple[str, ...] = (TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCEEDED, TASK_STATUS_PARTIAL, TASK_STATUS_FAILED)
ITEM_STATUS_PENDING = "PENDING"
ITEM_STATUS_RUNNING = "RUNNING"
ITEM_STATUS_DONE = "DONE"
ITEM_STATUS_SKIPPED = "SKIPPED"
ITEM_STATUS_FAILED = "FAILED"
COMPILE_ITEM_STATUSES: tuple[str, ...] = (ITEM_STATUS_PENDING, ITEM_STATUS_RUNNING, ITEM_STATUS_DONE, ITEM_STATUS_SKIPPED, ITEM_STATUS_FAILED)
STALE_RUNNING_MINUTES: int = 30

def _utcnow() -> datetime:
    return datetime.now(UTC)

class WikiCompileTask(Base):
    __tablename__ = "wiki_compile_task"
    __table_args__ = (
        Index("ix_wiki_compile_task_status", "status"),
        Index("ix_wiki_compile_task_created", "created_time"),
    )
    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    scope: Mapped[str | None] = mapped_column(String(30), nullable=True)
    selected_model_id: Mapped[int | None] = mapped_column(BigIntFk, ForeignKey("llm_config.id"), nullable=True)
    fallback_model_id: Mapped[int | None] = mapped_column(BigIntFk, ForeignKey("llm_config.id"), nullable=True)
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items: Mapped[list["WikiCompileItem"]] = relationship(back_populates="task", cascade="all, delete-orphan", passive_deletes=True, lazy="select")

class WikiCompileItem(Base):
    __tablename__ = "wiki_compile_item"
    __table_args__ = (
        Index("ix_wiki_compile_item_task_status", "task_id", "status"),
        UniqueConstraint("task_id", "page_id", name="uq_wiki_compile_item_task_page"),
    )
    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigIntFk, ForeignKey("wiki_compile_task.id", ondelete="CASCADE"), nullable=False)
    page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    mechanism_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    task: Mapped[WikiCompileTask] = relationship(back_populates="items", lazy="select")

__all__ = [
    "COMPILE_SCOPES", "COMPILE_TASK_STATUSES", "COMPILE_ITEM_STATUSES", "STALE_RUNNING_MINUTES",
    "TASK_STATUS_PENDING", "TASK_STATUS_RUNNING", "TASK_STATUS_SUCCEEDED", "TASK_STATUS_PARTIAL", "TASK_STATUS_FAILED",
    "ITEM_STATUS_PENDING", "ITEM_STATUS_RUNNING", "ITEM_STATUS_DONE", "ITEM_STATUS_SKIPPED", "ITEM_STATUS_FAILED",
    "WikiCompileTask", "WikiCompileItem",
]
