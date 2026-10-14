"""研究事件 outbox 服务（Task 12）：游标语义 / 序列化口径 / 失败不抛。

真实 PostgreSQL（unit/ 套件按 Harness 规则走真库）+ 真实 SQLAlchemy 会话。

钉住三条承重口径：
1. ``fetchEventsAfter`` 游标语义：``None`` ⇒ 空（**不**回放全量）；有游标 ⇒ 严格
   ``seq > cursor``（等于最大 seq 时返回空），且按会话隔离。
2. ``persistEvent`` 落库前必须过 ``research_json`` ⇒ ``Decimal -> str``（**不是** float）。
   反向：若走 ``_coerceJsonSafe``（Decimal -> float）则本断言失败 —— 本仓「两套 JSON 安全
   映射」的经典坑（memory ``qa-system-two-json-safe-mappings``）。
3. ``persistEvent`` 失败**不抛**（返回 ``None``，记 ERROR）—— 保住「落库失败不杀 turn」。
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchEventOutbox
from app.services.research_event_outbox import fetchEventsAfter, persistEvent

pytestmark = pytest.mark.asyncio


async def _seed(
    session: AsyncSession,
    sessionId: uuid.UUID,
    *,
    event: str = "research.intent",
    payload: dict[str, Any] | None = None,
) -> int:
    seq = await persistEvent(
        session, sessionId=sessionId, event=event, payload=payload or {"a": 1}
    )
    await session.commit()
    assert seq is not None, "persistEvent 在健康会话上必须返回 seq"
    return seq


async def test_persist_event_returns_monotonic_seq(dbSession: AsyncSession) -> None:
    """连续落两行 ⇒ seq 严格递增（BIGSERIAL 单调游标，非 created_at）。"""
    sid = uuid.uuid4()
    first = await _seed(dbSession, sid, event="research.intent")
    second = await _seed(dbSession, sid, event="research.esl")
    assert isinstance(first, int) and isinstance(second, int)
    assert second > first


async def test_fetch_events_after_none_returns_empty(dbSession: AsyncSession) -> None:
    """``afterSeq is None`` ⇒ 空列表（无游标的首次连接没有「错过」的概念）。"""
    sid = uuid.uuid4()
    await _seed(dbSession, sid)
    assert await fetchEventsAfter(dbSession, sid, None) == []


async def test_fetch_events_after_cursor_is_strictly_greater(dbSession: AsyncSession) -> None:
    """游标严格 ``>``：等于某 seq 时不返回该行；返回列表按 seq 升序。"""
    sid = uuid.uuid4()
    first = await _seed(dbSession, sid, event="research.intent")
    second = await _seed(dbSession, sid, event="research.esl")
    third = await _seed(dbSession, sid, event="research.plan")

    rowsAfterFirst = await fetchEventsAfter(dbSession, sid, first)
    assert [row.seq for row in rowsAfterFirst] == [second, third]

    rowsAfterSecond = await fetchEventsAfter(dbSession, sid, second)
    assert [row.seq for row in rowsAfterSecond] == [third]

    # 游标等于最大 seq ⇒ 空（严格 >，不是 >=）
    assert await fetchEventsAfter(dbSession, sid, third) == []


async def test_fetch_events_after_is_scoped_to_session(dbSession: AsyncSession) -> None:
    """按会话隔离：只返回目标会话的行，别的会话再新也不混入。"""
    sidA, sidB = uuid.uuid4(), uuid.uuid4()
    seqA = await _seed(dbSession, sidA, event="research.intent")
    await _seed(dbSession, sidB, event="research.intent")

    rows = await fetchEventsAfter(dbSession, sidA, seqA - 1)
    assert [row.seq for row in rows] == [seqA]


async def test_persist_event_serializes_decimal_as_string(dbSession: AsyncSession) -> None:
    """落库前过 ``research_json`` ⇒ ``Decimal`` 存成 JSON **字符串**（不是 float）。

    承重：实时帧是 ``research_json`` 的 ``Decimal -> str``；若落库走 ``_coerceJsonSafe``
    （``Decimal -> float``），回放帧的数值类型就与实时帧漂移。此断言若走 float 必失败。
    """
    sid = uuid.uuid4()
    payload = {"amount": Decimal("1.2")}
    await _seed(dbSession, sid, event="research.step.data", payload=payload)

    row = (
        await dbSession.execute(
            select(ResearchEventOutbox).where(ResearchEventOutbox.session_id == sid)
        )
    ).scalars().one()
    assert row.payload["amount"] == "1.2"
    assert not isinstance(row.payload["amount"], float)
    # 不可变：传入的 payload 不得被原地改写
    assert isinstance(payload["amount"], Decimal)


class _FailingSession:
    """``add`` 即抛的会话替身：模拟落库（含序列化 / 连接）失败。"""

    def add(self, _obj: Any) -> None:
        raise RuntimeError("outbox 落库失败（模拟）")

    async def flush(self) -> None:  # pragma: no cover —— add 已抛，不会走到
        raise RuntimeError("outbox 落库失败（模拟）")


async def test_persist_event_returns_none_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """落库失败 ⇒ 记 ERROR 且返回 ``None``（绝不冒泡，保住 turn）。"""
    with caplog.at_level(logging.ERROR):
        result = await persistEvent(
            _FailingSession(),  # type: ignore[arg-type]
            sessionId=uuid.uuid4(),
            event="research.intent",
            payload={"a": 1},
        )

    assert result is None
    assert caplog.text  # 至少有一条 ERROR 记账
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
