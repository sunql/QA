"""Task 12：研究事件 outbox 持久化 + SSE ``lastEventId`` 断线回放（真实 PG + 完整 HTTP 链路）。

承重集成点：turn 的每个事件**先落** ``research_event_outbox``（BIGSERIAL ``seq`` 单调游标）
**再**广播；SSE 端点 ``GET /research/stream`` 支持 ``?lastEventId=<seq>`` 回放其后事件，
每帧带 ``id: <seq>`` 行。

驱动方式：复用 ``test_research_stream.py:_fullService``（autoConfirm 全跑管线 + 真实
``ReportPlanner``）；httpx ``ASGITransport`` 下 BackgroundTasks 于 ``client.post`` 返回前
跑完 ⇒ turn 落库、outbox 落行都已完成，可直接断言。

场景：
1. 落库 + seq 单调无重复；
2. 有游标回放**恰好**是 ``seq > cursor`` 的事件，每帧带递增 ``id:``；
3. 无游标**不**回放历史（只 connected + 之后的实时事件）；
4. 序列化口径一致（承重）：``Decimal`` 回放出来是 **JSON 字符串**（不是 float）；
5. 坏帧（``bytes``）不截流（后续帧仍到达 + 日志 ERROR）；
6. 只在测试库（``SELECT current_database()`` 即 ``qa_metadata_test``）。

Task 12 fix round 1（两条「仅当客户端真发 ``lastEventId`` 才发作」的正确性缺陷）：
7. **多轮不早退**（缺陷 1 正向守卫）：第 1 轮 ``done`` 之后起的第 2 轮，重连回放**不得**
   停在第 1 轮的终态上（反向守卫：改回「回放中任意终态即 return」必失败）；
8. **确实结束后仍收尾**（防过度修复）：单轮跑完、游标在终态之前 ⇒ 回放含终态 + 会话
   已是终态 ⇒ 流正常结束（不挂到心跳超时）；
9. **游标已最新时不误关流**（保「先连接再提交」）：``lastEventId`` = 最后一条 seq ⇒ 回放
   为空 ⇒ 不得因状态判断被立刻关掉，且能收到之后实时发布的事件；
10. **回放翻页**（缺陷 2）：页大小注入为 2 ⇒ 5 条错失事件全部翻回（反向守卫：单次查询必失败）；
11. ``_isSessionTerminal`` 三态：``done``/``failed``/``aborted`` ⇒ True；``running``/
    ``awaiting_user`` ⇒ False；行不存在 ⇒ False。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.domain.research_models import ResearchEventOutbox
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.services.research_agent_ports import (
    EVENT_DONE,
    EVENT_STEP_DATA,
    EVENT_STEP_START,
)
from app.services.research_event_bus import EVENT_CONNECTED
from app.tests.integration.test_research_agent_service import FakeRunner
from app.tests.integration.test_research_stream import _fullService

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"
_QUESTION = "供应商收货量为什么下降"
# 回放请求应在「重放完终态事件即关流」下秒回；超时即视为「端点未实现回放」的 RED。
_STREAM_TIMEOUT = 8.0
_WAIT_SUBSCRIBER_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# 夹具（镜子 test_evidence_contract_e2e.py：research 专属夹具按文件定义）
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        name=f"event-outbox-oracle-{uuid.uuid4().hex[:8]}",
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=True,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    dbSession.add(
        User(
            username="event-outbox-user",
            display_name="outbox",
            email=None,
            enabled=True,
            password_hash=None,
        )
    )
    await dbSession.commit()
    await _seedDatasource(dbSession)
    return {"X-User-Id": "event-outbox-user"}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _createSession(client: AsyncClient, headers: dict[str, str]) -> str:
    """建会话（不触发 turn）；outbox 初始为空。"""
    created = await client.post(
        f"{_BASE}/sessions", json={"question": _QUESTION}, headers=headers
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _postTurn(client: AsyncClient, headers: dict[str, str], sid: str) -> None:
    """追加一轮（202）；ASGITransport 下后台状态机在响应返回前跑完。"""
    accepted = await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": _QUESTION}, headers=headers
    )
    assert accepted.status_code == 202, accepted.text


async def _runFullTurn(client: AsyncClient, headers: dict[str, str]) -> str:
    """建会话 + 跑满一轮（首轮）。"""
    sid = await _createSession(client, headers)
    await _postTurn(client, headers, sid)
    return sid


async def _seedOutbox(dbSession: AsyncSession, sid: str, count: int) -> list[int]:
    """直写 ``research_event_outbox``（真实 PG，不经业务假替身），返回新种入的 seq 列表。"""
    dbSession.add_all(
        [
            ResearchEventOutbox(
                session_id=uuid.UUID(sid), event=EVENT_STEP_START, payload={"i": index}
            )
            for index in range(count)
        ]
    )
    await dbSession.commit()
    rows = await _outboxRows(dbSession, sid)
    return [seq for seq, _event, _payload in rows]


async def _setSessionStatus(dbSession: AsyncSession, sid: str, status: str) -> None:
    """直接把会话置成指定状态（真实 PG）——覆盖 ``_isSessionTerminal`` 的各个取值。"""
    await dbSession.execute(
        text("UPDATE research_session SET status = :status WHERE id = :sid"),
        {"status": status, "sid": sid},
    )
    await dbSession.commit()


async def _outboxRows(dbSession: AsyncSession, sid: str) -> list[tuple[int, str, dict[str, Any]]]:
    """直查 ``research_event_outbox``（独立于生产读取器 —— 真交叉检验）。"""
    result = await dbSession.execute(
        text(
            "SELECT seq, event, payload FROM research_event_outbox "
            "WHERE session_id = :sid ORDER BY seq ASC"
        ),
        {"sid": sid},
    )
    return [(int(seq), event, payload) for seq, event, payload in result.all()]


def _parseFramesWithIds(body: str) -> list[dict[str, Any]]:
    """解析 SSE 文本：跳过心跳注释行，抽 ``event:`` / ``id:`` / ``data:``。"""
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        if not lines or lines[0].startswith(":"):
            continue
        name = next((ln[len("event: ") :] for ln in lines if ln.startswith("event: ")), None)
        if name is None:
            continue
        data = next((ln[len("data: ") :] for ln in lines if ln.startswith("data: ")), None)
        rawId = next((ln[len("id: ") :] for ln in lines if ln.startswith("id: ")), None)
        frames.append(
            {
                "event": name,
                "data": json.loads(data) if data else {},
                "id": int(rawId) if rawId is not None else None,
            }
        )
    return frames


async def _waitForSubscriber(sid: str, request: asyncio.Task[Any]) -> None:
    """等端点真的订阅上总线（否则 publish 打在空集上，用例会假绿）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_SUBSCRIBER_TIMEOUT
    while loop.time() < deadline:
        if researchModule._bus.subscriberCount(sid) >= 1:
            return
        if request.done():
            raise AssertionError(f"SSE 请求在订阅前就结束了: {request.exception() or request.result().text}")
        await asyncio.sleep(0.01)
    raise AssertionError(f"SSE 端点未在 {_WAIT_SUBSCRIBER_TIMEOUT}s 内订阅会话 {sid}")


# ---------------------------------------------------------------------------
# 1. 落库 + seq 单调
# ---------------------------------------------------------------------------


async def test_turn_persists_outbox_with_monotonic_seq(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跑完一个 turn ⇒ outbox 有 N 行，seq 严格递增且无重复。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)

    rows = await _outboxRows(dbSession, sid)
    assert rows, "turn 必须把事件落进 research_event_outbox"

    seqs = [seq for seq, _event, _payload in rows]
    assert seqs == sorted(seqs), f"seq 必须按升序返回: {seqs}"
    assert len(set(seqs)) == len(seqs), f"seq 必须无重复: {seqs}"
    assert all(isinstance(seq, int) for seq in seqs)
    # 事件族至少覆盖到终态 done（终态也必须可回放）
    assert any(event == EVENT_DONE for _seq, event, _payload in rows), [r[1] for r in rows]


# ---------------------------------------------------------------------------
# 2. 回放正确：恰好 seq > cursor
# ---------------------------------------------------------------------------


async def test_replay_returns_exactly_events_after_cursor(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?lastEventId=mid`` ⇒ 帧恰好是 ``seq > mid`` 的事件，每帧带递增 ``id:`` 行。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)

    rows = await _outboxRows(dbSession, sid)
    seqs = [seq for seq, _event, _payload in rows]
    assert len(seqs) >= 3, f"事件太少无法取中间游标: {seqs}"
    mid = seqs[len(seqs) // 2]
    expectedSeq = [seq for seq in seqs if seq > mid]

    resp = await asyncio.wait_for(
        client.get(
            f"{_BASE}/stream",
            params={"sessionId": sid, "lastEventId": mid},
            headers=authHeaders,
        ),
        timeout=_STREAM_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    frames = _parseFramesWithIds(resp.text)

    assert frames[0]["event"] == EVENT_CONNECTED
    replayFrames = frames[1:]
    # 每帧都带 id:，且 id 恰好是 seq > mid 的那些、严格递增
    assert all(frame["id"] is not None for frame in replayFrames), replayFrames
    assert [frame["id"] for frame in replayFrames] == expectedSeq


# ---------------------------------------------------------------------------
# 3. 无游标不回放
# ---------------------------------------------------------------------------


async def test_no_cursor_does_not_replay_history(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lastEventId=None`` ⇒ 只有 connected + 之后的实时事件，**不**重放陈年事件。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)
    assert await _outboxRows(dbSession, sid), "前置：库里已有历史事件（否则本用例形同虚设）"

    task = asyncio.create_task(client.get(f"{_BASE}/stream", params={"sessionId": sid}, headers=authHeaders))
    try:
        await _waitForSubscriber(sid, task)
        # 只有实时发布的这一帧（未落库、无 id）会到；历史事件**不得**被回放。
        await researchModule._bus.publish(sid, EVENT_DONE, {"degraded": False})
        resp = await asyncio.wait_for(task, timeout=_STREAM_TIMEOUT)
    finally:
        if not task.done():
            task.cancel()

    frames = _parseFramesWithIds(resp.text)
    assert [frame["event"] for frame in frames] == [EVENT_CONNECTED, EVENT_DONE], frames
    assert all(frame["id"] is None for frame in frames), "无游标连接不得产出带 id 的历史帧"


# ---------------------------------------------------------------------------
# 4. 序列化口径一致（承重）
# ---------------------------------------------------------------------------


async def test_replayed_decimal_stays_json_string(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带 ``Decimal`` 的步数据经 emit 落库 ⇒ 回放帧里是 **JSON 字符串**（不是 float）。

    承重（memory ``qa-system-two-json-safe-mappings``）：若落库走 ``_coerceJsonSafe``
    （``Decimal -> float``），则回放帧的 ``amount`` 会是 ``1.2``（number），与实时帧的
    ``"1.2"``（string）漂移 —— 本断言是那条回归的唯一守卫。
    """
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda **kw: _fullService(runner=FakeRunner(rows=[{"amount": Decimal("1.2")}]), **kw),
    )
    sid = await _runFullTurn(client, authHeaders)

    rows = await _outboxRows(dbSession, sid)
    stepDataRows = [(seq, payload) for seq, event, payload in rows if event == EVENT_STEP_DATA]
    assert stepDataRows, "本 turn 应发出 research.step.data"

    minSeq = min(seq for seq, _event, _payload in rows)
    resp = await asyncio.wait_for(
        client.get(
            f"{_BASE}/stream",
            params={"sessionId": sid, "lastEventId": minSeq - 1},
            headers=authHeaders,
        ),
        timeout=_STREAM_TIMEOUT,
    )
    frames = _parseFramesWithIds(resp.text)
    stepFrames = [frame for frame in frames if frame["event"] == EVENT_STEP_DATA]
    assert stepFrames, "回放应含 research.step.data 帧（否则回放没跑）"

    amount = stepFrames[0]["data"]["data"][0]["amount"]
    assert amount == "1.2", f"回放数值必须是 JSON 字符串，实际 {type(amount).__name__}: {amount!r}"
    assert not isinstance(amount, float), "回放数值不得是 float（两套 JSON 安全映射的坑）"


# ---------------------------------------------------------------------------
# 5. 坏帧不截流
# ---------------------------------------------------------------------------


async def test_bad_frame_does_not_truncate_stream(
    client: AsyncClient,
    authHeaders: dict[str, str],
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一帧 ``bytes``（``research_json`` 白名单外）不得炸掉整条 SSE 流。

    反向守卫：删掉 ``_sseFrame`` 的 try/except 守卫 ⇒ 生成器抛错 ⇒ 流被截断、后续
    ``research.done`` 收不到 ⇒ 本用例失败。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)

    task = asyncio.create_task(client.get(f"{_BASE}/stream", params={"sessionId": sid}, headers=authHeaders))
    try:
        await _waitForSubscriber(sid, task)
        with caplog.at_level(logging.ERROR, logger="app.api.v1.research"):
            await researchModule._bus.publish(sid, EVENT_STEP_DATA, {"index": 0, "blob": b"\x00\x01"})
            await researchModule._bus.publish(sid, EVENT_DONE, {"degraded": False})
            resp = await asyncio.wait_for(task, timeout=_STREAM_TIMEOUT)
    finally:
        if not task.done():
            task.cancel()

    frames = _parseFramesWithIds(resp.text)
    events = [frame["event"] for frame in frames]
    # 坏帧（step.data）在 done 之前，且两帧都在 ⇒ 坏帧没有截断流
    assert EVENT_STEP_DATA in events, f"坏帧必须仍被送达（降级）：{events}"
    assert events[-1] == EVENT_DONE, f"坏帧后续帧必须仍到达（流未被截断）: {events}"
    assert events.index(EVENT_STEP_DATA) < events.index(EVENT_DONE)
    assert any(record.levelno >= logging.ERROR for record in caplog.records), caplog.text


# ---------------------------------------------------------------------------
# 6. 只在测试库
# ---------------------------------------------------------------------------


async def test_outbox_lives_in_dedicated_test_db(dbSession: AsyncSession) -> None:
    """断连目标恒为独立测试库（``qa_metadata_test``），绝不落到生产 5433。"""
    currentDb = (await dbSession.execute(text("SELECT current_database()"))).scalar_one()
    assert currentDb == "qa_metadata_test", f"测试跑在非测试库上: {currentDb!r}"
    # 表在库中可见（迁移已应用）
    count = (
        await dbSession.execute(text("SELECT count(*) FROM research_event_outbox"))
    ).scalar_one()
    assert isinstance(count, int)


# ---------------------------------------------------------------------------
# 7. 缺陷 1 正向守卫：多轮会话不因**上一轮**的终态早退
# ---------------------------------------------------------------------------


async def test_replay_survives_previous_turn_terminal(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两轮会话：游标停在第 1 轮 ``done`` 之前 ⇒ 回放必须**越过**它，把第 2 轮事件也送出。

    outbox 按**会话**累计（无 run 作用域），``_isTerminal`` 认**任意** ``research.done``。
    若回放循环里「撞到终态帧就 return」，第 2 轮的事件一条都收不到、UI 永远转圈
    —— 这正是断线重连要解决的场景本身。

    反向守卫：把 ``_eventFrames`` 的回放循环改回「回放中任意终态即 return」，本用例必失败。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)
    await _postTurn(client, authHeaders, sid)

    rows = await _outboxRows(dbSession, sid)
    seqs = [seq for seq, _event, _payload in rows]
    doneSeqs = [seq for seq, event, _payload in rows if event == EVENT_DONE]
    assert len(doneSeqs) >= 2, f"前置：两轮应各产出一次 {EVENT_DONE}: {doneSeqs}"
    firstDone = doneSeqs[0]
    expected = [seq for seq in seqs if seq > firstDone - 1]
    assert max(expected) > firstDone, f"前置：第 2 轮应产出 seq > 第 1 轮 done 的事件: {seqs}"

    resp = await asyncio.wait_for(
        client.get(
            f"{_BASE}/stream",
            params={"sessionId": sid, "lastEventId": firstDone - 1},
            headers=authHeaders,
        ),
        timeout=_STREAM_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    frames = _parseFramesWithIds(resp.text)
    ids = [frame["id"] for frame in frames[1:]]

    assert ids == expected, f"回放不得停在第 1 轮的终态上（缺陷 1）: {ids} vs {expected}"
    assert max(ids) > firstDone, f"第 2 轮事件必须被回放出来: {ids}"


# ---------------------------------------------------------------------------
# 8. 防过度修复：会话确实结束后回放仍收尾
# ---------------------------------------------------------------------------


async def test_replay_closes_when_session_truly_done(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单轮跑完、游标在终态之前 ⇒ 回放含终态 + 会话已终态 ⇒ 流立即收尾（不挂心跳）。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)

    rows = await _outboxRows(dbSession, sid)
    doneSeq = next(seq for seq, event, _payload in rows if event == EVENT_DONE)

    resp = await asyncio.wait_for(
        client.get(
            f"{_BASE}/stream",
            params={"sessionId": sid, "lastEventId": doneSeq - 1},
            headers=authHeaders,
        ),
        timeout=_STREAM_TIMEOUT,
    )
    frames = _parseFramesWithIds(resp.text)

    assert frames[-1]["event"] == EVENT_DONE, frames
    assert ": ping" not in resp.text, "终态回放后应立刻收尾，不得挂到心跳超时"


# ---------------------------------------------------------------------------
# 9. 防过度修复：游标已最新（回放为空）时不得误关流
# ---------------------------------------------------------------------------


async def test_empty_replay_does_not_close_stream(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lastEventId`` = 最后一条 seq ⇒ 回放为空 ⇒ 不得因「会话已终态」被立刻关流。

    这保住「对已结束的会话先连接、再 POST 新 turn」这类正常流程：此刻游标 >= 上一轮
    最后的 seq，回放为空 —— 若把状态判断挪到 ``frames`` 为空的路径上，连接会被误关。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _runFullTurn(client, authHeaders)

    rows = await _outboxRows(dbSession, sid)
    lastSeq = max(seq for seq, _event, _payload in rows)

    task = asyncio.create_task(
        client.get(
            f"{_BASE}/stream",
            params={"sessionId": sid, "lastEventId": lastSeq},
            headers=authHeaders,
        )
    )
    try:
        await _waitForSubscriber(sid, task)
        # 回放为空 ⇒ 应进入实时循环；此时发布的事件必须到达（否则是被误关流）
        await researchModule._bus.publish(sid, EVENT_DONE, {"degraded": False})
        resp = await asyncio.wait_for(task, timeout=_STREAM_TIMEOUT)
    finally:
        if not task.done():
            task.cancel()

    frames = _parseFramesWithIds(resp.text)
    assert [frame["event"] for frame in frames] == [EVENT_CONNECTED, EVENT_DONE], frames
    assert all(frame["id"] is None for frame in frames[1:]), "回放为空 ⇒ 无带 id 的历史帧"


# ---------------------------------------------------------------------------
# 10. 缺陷 2：回放翻页直到取不满一页
# ---------------------------------------------------------------------------


async def test_fetch_missed_pages_until_short_page(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
) -> None:
    """页大小注入为 2 ⇒ 5 条错失事件必须**全部**翻回来（单次查询只回 2 条 ⇒ 静默丢 3 条）。

    反向守卫：把 ``_fetchMissed`` 改回单次 ``fetchEventsAfter`` 调用，本用例必失败。
    """
    sid = await _createSession(client, authHeaders)
    seeded = await _seedOutbox(dbSession, sid, count=5)
    assert len(seeded) == 5, f"前置：应种入 5 条: {seeded}"

    missed = await researchModule._fetchMissed(sid, seeded[0] - 1, limit=2)

    got = [seq for _event, _payload, seq in missed]
    assert got == seeded, f"翻页必须取回全部 5 条: {got}"
    assert got == sorted(got), f"回放 id 必须递增: {got}"


# ---------------------------------------------------------------------------
# 11. _isSessionTerminal 三态
# ---------------------------------------------------------------------------


async def test_is_session_terminal_matches_session_status(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
) -> None:
    """只有 ``done``/``failed``/``aborted`` 是终态；``running``/``awaiting_user`` 不是。

    ``awaiting_user`` 被误判为终态会让「等待用户答 checkpoint」的会话瞬间被关流。
    """
    sid = await _createSession(client, authHeaders)

    for status in ("running", "awaiting_user"):
        await _setSessionStatus(dbSession, sid, status)
        assert await researchModule._isSessionTerminal(sid) is False, status

    for status in ("done", "failed", "aborted"):
        await _setSessionStatus(dbSession, sid, status)
        assert await researchModule._isSessionTerminal(sid) is True, status

    # 行不存在 ⇒ False（宁可多连一会儿，不可误关流）
    assert await researchModule._isSessionTerminal(str(uuid.uuid4())) is False
