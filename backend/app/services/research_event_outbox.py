"""研究事件 outbox（Task 12）：事件**先落库、再广播** ⇒ SSE 断线可按游标回放。

为什么单开此表 / 服务（而非复用内存总线）：进程内总线（``research_event_bus``）只投递
**在线**订阅者、**不留历史** —— 页面刷新 / SSE 断线后错过的事件永久丢失。outbox 把每个
事件连同单调 ``seq`` 落库，SSE 端点据此回放 ``seq > lastEventId`` 的事件。

**载荷必须先过 ``research_json`` 再落库（承重，勿改）**：本仓有**两套** JSON 安全映射
（memory ``qa-system-two-json-safe-mappings``）—— ``_coerceJsonSafe``（``Decimal`` →
**float**、未知类型**静默**字符串化）vs ``research_json``（``Decimal`` → **str**、未知类型
``TypeError``）。若走 SQLAlchemy 默认 json 序列化或 ``_coerceJsonSafe``，回放出来的
``Decimal`` 是 **float**，而**实时**帧（``_sseFrame`` → ``research_json``）是 **str** ⇒
同一条事件「重连看到的值」与「当时看到的值」类型不同，前端解析漂移。先 ``research_json``
再 ``json.loads`` 让落库值就是线上的值；回放时 ``research_json`` 二次编码是**幂等**的。

失败口径与 ``emitEvent`` 一致：落库失败**不抛**（记 ERROR 后返回 ``None``），不得让异常
冒泡 —— 保住「推流 / 记账失败不杀 turn」。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchEventOutbox
from app.services.research_json_encoder import research_json

logger = logging.getLogger(__name__)

_REPLAY_LIMIT = 1000
"""单次回放上限（防御无界回放；超限由客户端按最后一帧的 ``id`` 续拉）。"""


async def persistEvent(
    session: AsyncSession,
    *,
    sessionId: uuid.UUID,
    event: str,
    payload: dict[str, Any],
) -> int | None:
    """落一行事件台账，返回其 ``seq``；失败**不抛**（记 ERROR 后返回 ``None``）。

    ``payload`` 先过 ``research_json``（``Decimal``→str / ``date``→ISO / ``UUID``→str；
    非法类型抛错），再 ``json.loads`` 回 dict 落 JSONB —— 使落库值 == 线上帧值（见模块
    docstring）。**不**原地改传入的 ``payload``（``research_json``/``json.loads`` 均不改变入参）。
    """
    try:
        safePayload = json.loads(research_json(payload))
    except Exception:  # noqa: BLE001 —— 非法载荷：记 ERROR 并跳过落库，不杀 turn
        logger.exception(
            "研究事件载荷无法序列化，跳过 outbox: session=%s event=%s", sessionId, event
        )
        return None
    try:
        row = ResearchEventOutbox(session_id=sessionId, event=event, payload=safePayload)
        session.add(row)
        await session.flush()
        return int(row.seq)
    except Exception:  # noqa: BLE001 —— 落库失败：记 ERROR，返回 None（调用方据此不写 id:）
        logger.exception("研究事件 outbox 落库失败: session=%s event=%s", sessionId, event)
        return None


async def fetchEventsAfter(
    session: AsyncSession,
    sessionId: uuid.UUID,
    afterSeq: int | None,
    limit: int = _REPLAY_LIMIT,
) -> list[ResearchEventOutbox]:
    """取该会话 ``seq > afterSeq`` 的事件（升序，至多 ``limit`` 条）。

    ``afterSeq is None`` ⇒ 空列表（**不回放全量**）：无游标的首次连接没有「错过」的概念，
    回放整段历史会把陈年事件当新事件重放。有游标则严格 ``seq > afterSeq``（不是 ``>=``），
    按 ``seq`` 升序返回。
    """
    if afterSeq is None:
        return []
    result = await session.execute(
        select(ResearchEventOutbox)
        .where(
            ResearchEventOutbox.session_id == sessionId,
            ResearchEventOutbox.seq > afterSeq,
        )
        .order_by(ResearchEventOutbox.seq.asc())
        .limit(limit)
    )
    return list(result.scalars().all())
