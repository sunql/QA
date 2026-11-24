"""I3 多轮 Copilot 会话 ORM（会话壳 + 轮次映射）。

为什么需要这两张表（而不是往 `agent_orchestration_run` 加列）：
- `agent_orchestration_run` 是**编排域**的共享表（P3/P5 都在用），为一个入口加
  `conversation_id` 会把「会话」概念反向污染到所有 run；
- 会话本身有独立的生命周期与归属（谁开的、绑哪个 Agent/模型、多少轮），
  与 run 的「一次执行」不是同一个东西。

轮次表把「用户发了什么」与「哪次 run 回答的」分开存：
- 追问（PAUSED/NEEDS_INPUT）时，下一句话会 **resume 同一个 run** ⇒ 两条轮次
  指向同一个 `run_id`，历史不被改写；
- run 的 `question` 会被 resume 追加 `[补充]`，而界面展示用轮次表里的原文。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntPk, TimestampMixin

#: 会话轮数上限（超过后必须开新会话；防止单会话无限增长）。
MAX_TURNS_PER_CONVERSATION = 50


class ProcurementCopilotConversation(Base, TimestampMixin):
    """一次 Copilot 会话（多轮对话的归属与绑定）。"""

    __tablename__ = "procurement_copilot_conversation"
    __table_args__ = (
        sa.Index("uq_copilot_conversation_id", "conversation_id", unique=True),
        sa.Index("ix_copilot_conversation_owner", "created_by"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    conversation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_by: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: 会话内 Agent 与模型**绑定**（中途换场景 = 新会话，避免上下文串味）。
    agent_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    model_config_id: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # 会话置顶（跨设备同步，0170）：NULL = 未置顶；非 NULL = 置顶时间
    pinned_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class ProcurementCopilotTurn(Base, TimestampMixin):
    """一条用户消息（轮次）及其对应的编排运行。"""

    __tablename__ = "procurement_copilot_turn"
    __table_args__ = (
        sa.UniqueConstraint("conversation_id", "seq", name="uq_copilot_turn_seq"),
        sa.Index("ix_copilot_turn_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    conversation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    question: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: 回答这一轮的用户消息的运行（追问时与上一轮相同）
    run_id: Mapped[str] = mapped_column(sa.String(36), nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
