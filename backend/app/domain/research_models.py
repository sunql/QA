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
_STEP_TYPE_LEN = 50
_EVENT_LEN = 50
"""事件名列长（最长事件名 ``research.step.start`` = 19 字符；留余量，与既有 ``_*_LEN`` 同风格）。"""


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
    # 会话级执行权互斥（Task 2）：非 NULL = 有运行正在进行（或等待用户决策）。
    # 指向「本轮运行的初始 run_state 行」；run state 被版本化后仍保持初始 id
    # （释放判据是会话终态，不是 id 相等 —— 见 `releaseActiveRun`）。
    # ondelete=SET NULL：run state 被删（会话 CASCADE）时不该阻塞会话行本身。
    active_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_run_state.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
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


class ResearchRunState(Base):
    """研究会话单轮运行的版本化状态快照。"""

    __tablename__ = "research_run_state"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _session_fk(), nullable=False, index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), _turn_fk(), nullable=False, index=True
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_run_state.id", ondelete="SET NULL"),
        nullable=True,
    )
    current_question: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    mode: Mapped[str] = mapped_column(String(_MODE_LEN), nullable=False)
    scope_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    datasource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ResearchStepResult(Base):
    """研究计划单步的执行结果快照（Task 6）：SQL / 原始行引用 / 证据摘要 / 状态。

    挂在**当前版本**的 `research_run_state` 下 —— resume 时 `updateRunState` 生成新 UUID，
    步结果随之归属新版本（跨版本完整读取由 Task 8 按 `research_run_state.turn_id` join，
    见 brief D25；本轮写侧只挂 `state["runStateId"]`）。

    原始行放 `raw_result_ref`（JSONB，`{"rows": [...], "schemaVersion": 1}`，超
    `MAX_FINDING_ROWS` 截断）；证据摘要放 `evidence_summary`（已是 JSON 原生 dict）。
    """

    __tablename__ = "research_step_result"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_state_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_run_state.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(_STEP_TYPE_LEN), nullable=False)
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_result_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    evidence_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(_STATUS_LEN), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ResearchEventOutbox(Base):
    """研究事件 outbox（Task 12）：事件**先落库、再广播** ⇒ SSE 断线可回放。

    三处刻意偏离计划的裁定（逐条写清理由）：

    1. **游标用 ``BIGSERIAL seq`` 主键，不用 ``created_at``**。计划拟
       ``WHERE created_at > after_time``：同一毫秒的并列行会**漏读或重读**，且 DB 时钟
       回拨仍会乱序。``seq`` 单调递增 ⇒ ``WHERE seq > :cursor ORDER BY seq ASC`` 严格正确。
       SSE 的 ``lastEventId`` 就是 ``seq``。
    2. **不带 ``run_state_id``**：``emit`` 的 callable 在 ``runTurn`` 创建 run_state
       **之前**就已绑定（wrapper 层拿不到该 id），且回放查询是**按会话**（``session_id``）
       而非按版本 ⇒ 该列对回放零贡献，**不**为填它去改 ``runTurn`` 签名。
    3. **不带 ``id``/``event_id`` UUID**：``seq`` 已唯一且有序，再留一个 UUID 是冗余。
    """

    __tablename__ = "research_event_outbox"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(_EVENT_LEN), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
