"""环节二平台级集成验收（Task 16）：跨特性交汇点 e2e（真实 PG + 完整 HTTP 链路）。

T12（outbox 持久化 + `?lastEventId=` 回放）与 T15（会话级预算只读闸门）各自的**单独**
集成覆盖已很密（`test_event_outbox.py` / `test_session_budget_gate.py`），本文件**只**补
两个经 recon 确认**尚无任何既有用例断言**的**交汇点**（不重写既有场景）：

J1 —— 超预算终止帧**落 outbox 且可回放**（T15 × T12）：T15 在相位边界终止 turn 时发
`research.error{turn_failed}`。用户若在终止瞬间断线、之后再连，**只有**该 error 被持久化
到 `research_event_outbox`（带 `seq`）才能经 `GET /research/stream?lastEventId=` 回放；
否则重连客户端只看到一条**静默死流**（本项目反复踩的 SSE 静默失败类）。承重断言 =
**实时终态帧带 `id: <seq>`**（= 已落库）且该 `seq` 能回放出同一条 error 帧 —— 用「实时帧
自带的 seq」而非「随便找一条 outbox error」，正是为了钉住「**用户看到的那一帧**可回放」。

J2 —— 超预算终止后会话**可复用**，不是永久 409（T15 × 释放执行权不变式）：`failed` 是
终态 ⇒ 必须一并释放 `active_run_id`，否则超预算会话带着占用永久 409（新轮次再也提交不
进来）。承重断言 = 终止后直接查库 `active_run_id IS NULL`，且**同一会话**新 `POST /turns`
被受理（202），并按更高预算真跑到 `done`（证明是**真复用**，不是「受理后又立刻失败」）。

驱动方式复用既有一致范式：`httpx` `ASGITransport` 把整响应体缓冲 ⇒ **并发**发起 GET 流与
POST turn（先订阅、等总线订阅数 > 0，再触发 turn，见 `test_session_budget_gate.py`）。
预算经**构造器 seam** `sessionBudget=` 注入 —— **不**改 `getSettings()` 全局（conftest 全局
`SESSION_BUDGET=0.1`，改全局会污染其它用例）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, SessionTokenUsage
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.services.research_agent_ports import (
    ERROR_TURN_FAILED,
    EVENT_ERROR,
    EVENT_HYPOTHESIS,
    EVENT_STEP_DONE,
)
from app.services.research_agent_service import ResearchAgentService
from app.services.research_event_bus import EVENT_CONNECTED, bus
from app.services.research_session_service import ResearchSessionService
from app.services.token_usage_service import TokenUsageService
from app.tests.integration.test_research_agent_service import (
    HYPOTHESIS_JSON,
    QUESTION,
    FakeChart,
    FakeEsl,
    FakeModelConfigs,
    FakeModelRouter,
    FakeOntology,
    FakePlanner,
    FakeRunner,
    FakeUsageRecorder,
    ScriptedLlmClient,
)

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"
# 注入预算 0.1；种入成本 0.5 ⇒ 首个相位边界即超限终止。
_INJECTED_BUDGET = 0.1
_OVER_BUDGET_COST = Decimal("0.5")
# J2 专用：种入成本 **低于** 预算，由 `_PhaseBoundaryCost` 在后段抬高，让终止落在
# **execute 已提交之后** 的 hypothesis 边界（见该类的 docstring）。
_UNDER_BUDGET_SEED = Decimal("0.05")
# 复用轮次注入的**高**预算：闸门不触发，会话真跑到 done（证明「闸门不是永久锁」）。
_REUSABLE_BUDGET = 100.0
# 回放请求应在「重放完终态事件即关流」下秒回；超时即视为「端点未实现回放」的 RED。
_STREAM_TIMEOUT = 8.0

# `_runFrom` 在 **每个相位边界** 读一次会话成本。经实测，`hypothesis` 边界那次是第 6 次读
# （前 5 次：intent/esl/plan/execute 边界 + 相位内的一次路由上下文装配）⇒ 让成本在**第 6 次**
# 起超预算，终止即落在 `hypothesis` 边界 —— 此时 `execute` 相位已 `commit`，认领的执行权
# 已落库，释放才真正承重。若相位结构变化导致落点漂移，J2 里「终止在 hypothesis 边界」的
# 断言会**响亮失败**，不会静默失去本用例的意义。
_GATE_READS_BEFORE_HYPOTHESIS = 5


class _PhaseBoundaryCost:
    """会话成本读取 seam（`tokenUsage=`，闸门与路由上下文共用的公开读数面）。

    前 `lowReads` 次**透传真实** `TokenUsageService`（读的是种进 `session_token_usage` 的
    真实低成本）；之后返回 `high`（超预算）。用于把超预算终止**推迟**到 execute 相位已提交
    之后 —— 首个边界就终止时执行权从未落库，释放与否对 DB 无影响，反向守卫会假绿。
    """

    def __init__(self, *, lowReads: int, high: Decimal) -> None:
        self._real = TokenUsageService()
        self._lowReads = lowReads
        self._high = high
        self.reads = 0

    async def getSessionCost(self, session: AsyncSession, sessionId: str) -> Decimal:
        self.reads += 1
        if self.reads <= self._lowReads:
            return await self._real.getSessionCost(session, sessionId)
        return self._high

    async def getSessionTurnCount(self, session: AsyncSession, sessionId: str) -> int:
        return await self._real.getSessionTurnCount(session, sessionId)

    async def getLastModelId(self, session: AsyncSession, sessionId: str) -> Any:
        return await self._real.getLastModelId(session, sessionId)


# ---------------------------------------------------------------------------
# Arrange 辅助（镜子 test_session_budget_gate.py）
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    """种一个启用的默认业务源（建会话 fail-closed：解析不到即 422）。"""
    ds = DataSource(
        name=f"platform-e2e-{uuid.uuid4().hex[:8]}",
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
    await dbSession.refresh(ds)
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    dbSession.add(
        User(
            username="platform-e2e-user",
            display_name="pe",
            email=None,
            enabled=True,
            password_hash=None,
        )
    )
    await dbSession.commit()
    await _seedDatasource(dbSession)
    return {"X-User-Id": "platform-e2e-user"}


async def _seedUsage(dbSession: AsyncSession, sessionId: str, cost: Decimal) -> None:
    """种一行真实 `session_token_usage`（闸门的唯一数据源）。"""
    dbSession.add(
        SessionTokenUsage(
            session_id=sessionId,
            model_name="seed-model",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost=cost,
            purpose="seed",
        )
    )
    await dbSession.commit()


def _service(
    *, sessionBudget: float | None, tokenUsage: Any | None = None
) -> ResearchAgentService:
    """全跑管线（autoConfirm，零网络零 LLM）+ 注入 `sessionBudget` / `tokenUsage`。"""
    scripted = ScriptedLlmClient(HYPOTHESIS_JSON, "解读：收货量下降明显。")
    return ResearchAgentService(
        esl=FakeEsl(),
        sessionService=ResearchSessionService(),
        planner=FakePlanner(),
        runner=FakeRunner(),
        chartService=FakeChart(),
        llmFactory=lambda cfg: scripted,
        usageRecorder=FakeUsageRecorder(),
        modelConfigs=FakeModelConfigs(),
        modelRouter=FakeModelRouter(),
        ontology=FakeOntology(),
        autoConfirm=True,
        sessionBudget=sessionBudget,
        tokenUsage=tokenUsage,
    )


async def _createSession(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(f"{_BASE}/sessions", json={"question": QUESTION}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _waitForSubscriber(sessionId: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while bus.subscriberCount(sessionId) == 0:
        if loop.time() > deadline:
            raise AssertionError("SSE 流未在超时内完成订阅")
        await asyncio.sleep(0.01)


def _parseFramesWithIds(body: str) -> list[dict[str, Any]]:
    """解析 SSE 文本：跳过心跳注释行，抽 `event:` / `id:` / `data:`。"""
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


async def _runTurnCapturingStream(
    client: AsyncClient, headers: dict[str, str], sessionId: str
) -> list[dict[str, Any]]:
    """先订阅、再触发 turn，返回完整帧序（含每帧的 `id:`；ASGI 缓冲 ⇒ 返回即流已关闭）。"""
    stream = asyncio.create_task(
        client.get(f"{_BASE}/stream", params={"sessionId": sessionId}, headers=headers)
    )
    await _waitForSubscriber(sessionId)
    accepted = await client.post(
        f"{_BASE}/sessions/{sessionId}/turns", json={"question": QUESTION}, headers=headers
    )
    assert accepted.status_code == 202, accepted.text
    resp = await asyncio.wait_for(stream, timeout=30)
    assert resp.status_code == 200, resp.text
    return _parseFramesWithIds(resp.text)


async def _sessionStatus(client: AsyncClient, headers: dict[str, str], sessionId: str) -> str:
    detail = await client.get(f"{_BASE}/sessions/{sessionId}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()["session"]["status"]


async def _outboxRows(dbSession: AsyncSession, sid: str) -> list[tuple[int, str, dict[str, Any]]]:
    """直查 `research_event_outbox`（独立于生产读取器 —— 真交叉检验）。"""
    result = await dbSession.execute(
        text(
            "SELECT seq, event, payload FROM research_event_outbox "
            "WHERE session_id = :sid ORDER BY seq ASC"
        ),
        {"sid": uuid.UUID(sid)},
    )
    return [(int(seq), event, payload) for seq, event, payload in result.all()]


async def _activeRunId(dbSession: AsyncSession, sid: str) -> Any:
    """直查会话执行权占用（独立读取器 —— 不信任 API 层口径）。"""
    result = await dbSession.execute(
        text("SELECT active_run_id FROM research_session WHERE id = :sid"),
        {"sid": uuid.UUID(sid)},
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# J1 —— 超预算终止帧落 outbox 且可回放（T15 × T12）
# ---------------------------------------------------------------------------


async def test_over_budget_terminal_error_is_persisted_and_replayable(
    client: AsyncClient,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    dbSession: AsyncSession,
) -> None:
    """超预算终止的**实时终态帧**必须带 `id: <seq>`（已落 outbox）且该 seq 可回放。

    失败即：「终止帧不进台账 ⇒ 断线重连只剩静默死流」。反向守卫：把 `_runTurnInBackground`
    的 `emit=partial(_persistAndPublish, sessionId)` 改成直连 `bus.publish`（不落 outbox）
    ⇒ 实时终态帧的 `id:` 变 `None` ⇒ 本用例第一步断言即红。
    """
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_INJECTED_BUDGET),
    )
    sid = await _createSession(client, authHeaders)
    await _seedUsage(dbSession, sid, _OVER_BUDGET_COST)

    frames = await _runTurnCapturingStream(client, authHeaders, sid)

    # 1) 实时流：超预算在首个相位边界终止 ⇒ 末帧是终态 error（沿用 T15 既有口径）。
    assert frames[0]["event"] == EVENT_CONNECTED
    terminal = frames[-1]
    assert terminal["event"] == EVENT_ERROR
    assert terminal["data"]["code"] == ERROR_TURN_FAILED
    assert await _sessionStatus(client, authHeaders, sid) == "failed"

    # 2) 承重：实时终态帧必须带 `id: <seq>`（= 已落 outbox）。emit 若不落库 ⇒ 此断言红。
    terminalSeq = terminal["id"]
    assert terminalSeq is not None, "超预算终止帧必须落 outbox（帧上带 seq）"

    # 3) 独立直查 outbox：该会话确有一条 research.error 行，且 seq == 实时帧的 id。
    rows = await _outboxRows(dbSession, sid)
    errorSeqs = [seq for seq, event, _payload in rows if event == EVENT_ERROR]
    assert errorSeqs, "超预算终止帧必须落 research_event_outbox"
    assert terminalSeq in errorSeqs, f"实时终态 seq={terminalSeq} 不在 outbox: {errorSeqs}"

    # 4) `?lastEventId=<seq-1>` 回放 ⇒ 必须重放出这条 error 帧，且其 `id:` == seq。
    resp = await asyncio.wait_for(
        client.get(
            f"{_BASE}/stream",
            params={"sessionId": sid, "lastEventId": terminalSeq - 1},
            headers=authHeaders,
        ),
        timeout=_STREAM_TIMEOUT,
    )
    assert resp.status_code == 200, resp.text
    replayed = _parseFramesWithIds(resp.text)
    assert replayed[0]["event"] == EVENT_CONNECTED
    replayFrames = replayed[1:]
    assert any(
        frame["event"] == EVENT_ERROR
        and frame["id"] == terminalSeq
        and frame["data"].get("code") == ERROR_TURN_FAILED
        for frame in replayFrames
    ), f"回放未含实时终态帧 id={terminalSeq}: {replayFrames}"


# ---------------------------------------------------------------------------
# J2 —— 超预算终止后会话可复用，不是永久 409（T15 × 释放执行权不变式）
# ---------------------------------------------------------------------------


async def test_over_budget_abort_releases_session_for_reuse(
    client: AsyncClient,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    dbSession: AsyncSession,
) -> None:
    """超预算终止（failed 终态）必须释放 `active_run_id` ⇒ 同一会话新提交被受理（202）。

    失败即：「超预算会话带着占用永久 409（新轮次再也提交不进来）」。反向守卫：让
    `markFailed` **不**释放（改成只 `updateSessionStatus`、不清 `active_run_id`）⇒ 第 3 步
    的 202 必红（变 409）。

    **口径与理由（刻意偏离 brief 的「首边界即终止」写法）**：超预算若在**首个**相位边界终止，
    本轮的执行权（`active_run_id`）**从未被 commit** ⇒ 事务回滚后库里它本就是 NULL，释放与否
    对 DB 无影响 —— 那种写法下反向守卫会**假绿**（实测确认）。故本用例把终止**推迟**到
    `execute` 相位提交之后（`hypothesis` 边界，见 `_PhaseBoundaryCost`）：此时认领已落库，
    「终止必须释放」才真正承重。种入的成本**低于**预算，交 `_PhaseBoundaryCost` 在后段抬高。
    """
    lowCost = _PhaseBoundaryCost(
        lowReads=_GATE_READS_BEFORE_HYPOTHESIS, high=_OVER_BUDGET_COST
    )
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_INJECTED_BUDGET, tokenUsage=lowCost),
    )
    sid = await _createSession(client, authHeaders)
    await _seedUsage(dbSession, sid, _UNDER_BUDGET_SEED)  # 真实低成本行（前段透传读取）

    # 1) 超预算终止 ⇒ 会话 failed；且终止落在 **execute 提交之后**（否则本用例失去承重面）。
    frames = await _runTurnCapturingStream(client, authHeaders, sid)
    events = [frame["event"] for frame in frames]
    assert events[-1] == EVENT_ERROR
    assert EVENT_STEP_DONE in events, f"execute 相位必须已跑过（认领已提交）: {events}"
    assert EVENT_HYPOTHESIS not in events, f"终止须在 hypothesis 边界: {events}"
    assert await _sessionStatus(client, authHeaders, sid) == "failed"

    # 2) 前置（承重）：failed 是终态 ⇒ 执行权必须一并释放（且它此刻**确实**已在库里）。
    assert await _activeRunId(dbSession, sid) is None, "超预算终止必须释放 active_run_id"

    # 3) 闸门不是永久锁：同一会话新提交必须被**受理**（202，不是 409）。
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_REUSABLE_BUDGET, tokenUsage=lowCost),
    )
    accepted = await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": QUESTION}, headers=authHeaders
    )
    assert accepted.status_code == 202, accepted.text
    # 4) 真复用：本轮**跑到底**（若只是「受理后又立刻失败」则此处为 failed）。
    assert await _sessionStatus(client, authHeaders, sid) == "done"
