"""研究型入口 5 张表（feat-research-entry）。

与 chat 域完全隔离：不触碰 chat_session / session_message /
analysis_hypothesis / evidence。created_by 不加 FK（跨域不耦合）。

约定：ORM 列属性使用 snake_case，与 SQL 列名一一对应。
JSON 契约的 camelCase 由 Pydantic schema 的 alias_generator 负责。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base

_STATUS_LEN = 20
_MODE_LEN = 20
_PHASE_LEN = 30


def _utcnow() -> datetime:
    """时区感知的当前 UTC 时间（与 models.py 保持一致）。"""
    return datetime.now(UTC)


def _session_fk() -> ForeignKey:
    return ForeignKey("research_session.id", ondelete="CASCADE")


def _turn_fk() -> ForeignKey:
    return ForeignKey("research_turn.id", ondelete="CASCADE")


class ResearchSession(Base):
    """研究会话主表。"""

    __tablename__ = "research_session"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    mode: Mapped[str] = mapped_column(
        String(_MODE_LEN), default="research", server_default="research"
    )
    status: Mapped[str] = mapped_column(
        String(_STATUS_LEN), default="running", server_default="running"
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 业务数据源（0112）：研究是**多轮 + 可恢复**的，源在会话上一次性选定全程沿用
    # （state 不持久化，故不能只按请求带）。不加 FK / 可空，理由见迁移 0112。
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 会话级 LLM 模型选择（W5）：NULL = 自动路由。无 FK（与 datasource_id 同风格）——
    # 模型可被停用/删除，但历史会话要保留「当时用的是哪个模型」的事实。
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_seed: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ResearchTurn(Base):
    """研究会话内的单轮交互。"""

    __tablename__ = "research_turn"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _session_fk(), nullable=False, index=True
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ResearchCheckpoint(Base):
    """分阶段决策检查点（等待用户选择时挂起）。"""

    __tablename__ = "research_checkpoint"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _session_fk(), nullable=False, index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _turn_fk(), nullable=False, index=True
    )
    phase: Mapped[str] = mapped_column(String(_PHASE_LEN), nullable=False)
    status: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    user_choice: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ResearchFinding(Base):
    """研究过程中沉淀的结论/发现。"""

    __tablename__ = "research_finding"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _session_fk(), nullable=False, index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _turn_fk(), nullable=False, index=True
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    supporting_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ResearchReport(Base):
    """研究报告（version 递增；published 版本对 session 唯一）。"""

    __tablename__ = "research_report"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _session_fk(), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    rendered_md: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        Index(
            "uq_research_report_session_published",
            "session_id",
            unique=True,
            postgresql_where=sa_text("status = 'published'"),
        ),
    )
