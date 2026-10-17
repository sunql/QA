"""会话级预算只读闸门（Task 15）：真实 PG + 完整 HTTP 链路 + 真 SSE 事件序。

单测只证明「判定纯逻辑对」，证明不了**接线**：闸门若没挂进 `_runFrom` 的相位边界，
超预算会话会照常跑完 `done` —— 单测全绿、线上毫无差别。故此处从 `POST /sessions`
起跑、种真实 `session_token_usage` 行、读真 SSE 帧序，断言：

1. 超预算 ⇒ 在**第一个相位边界**即终止（SSE 末帧 `research.error{turn_failed}`、
   消息带真实成本/预算、**无** `research.done`、会话落 `failed`）；
2. 不误伤：成本低于预算的正常会话照常 `done`；
3. **改模型零发生**（承重）：闸门路径**不**触碰模型路由 —— 「终止」与「换模型」正交
   （SSOT：memory `qa-system-no-model-override`，用户原话「选择什么模型就用什么模型」）。

预算经构造器 seam `sessionBudget=` 注入（**不**改 `getSettings()` 全局缓存 —— 那会
污染其它用例；conftest 全局 `SESSION_BUDGET=0.1`）。

驱动方式与 `test_research_stream.py` 同款：httpx ASGITransport 缓冲整个响应体，故
**并发**发起 GET 流与 POST turn（先订阅 + 等总线订阅数 > 0，再触发 turn）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource, SessionTokenUsage
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.services.research_agent_ports import TERMINAL_ERROR_CODES
from app.services.research_agent_service import ResearchAgentService
from app.services.research_event_bus import bus
from app.services.research_session_service import ResearchSessionService
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

_BASE = "/api/v1/research"
_INJECTED_BUDGET = 0.1
_OVER_BUDGET_COST = Decimal("0.5")
_UNDER_BUDGET_COST = Decimal("0.01")


# ---------------------------------------------------------------------------
# Arrange 辅助
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    """种一个启用的默认业务源（建会话 fail-closed：解析不到即 422）。"""
    ds = DataSource(
        name=f"budget-gate-{uuid.uuid4().hex[:8]}",
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
            username="budget-gate-user",
            display_name="bg",
            email=None,
            enabled=True,
            password_hash=None,
        )
    )
    await dbSession.commit()
    await _seedDatasource(dbSession)
    return {"X-User-Id": "budget-gate-user"}


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


def _service(*, sessionBudget: float | None, **kwargs: Any) -> ResearchAgentService:
    """全跑管线（autoConfirm，零网络零 LLM）+ 注入 `sessionBudget`。"""
    scripted = ScriptedLlmClient(HYPOTHESIS_JSON, "解读：收货量下降明显。")
    return ResearchAgentService(
        esl=kwargs.get("esl", FakeEsl()),
        sessionService=ResearchSessionService(),
        planner=kwargs.get("planner", FakePlanner()),
        runner=kwargs.get("runner", FakeRunner()),
        chartService=kwargs.get("chartService", FakeChart()),
        llmFactory=kwargs.get("llmFactory", lambda cfg: scripted),
        usageRecorder=kwargs.get("usageRecorder", FakeUsageRecorder()),
        modelConfigs=kwargs.get("modelConfigs", FakeModelConfigs()),
        modelRouter=kwargs.get("modelRouter", FakeModelRouter()),
        ontology=kwargs.get("ontology", FakeOntology()),
        tokenUsage=kwargs.get("tokenUsage"),
        autoConfirm=True,
        sessionBudget=sessionBudget,
    )


class _FlakyTokenUsage:
    """前 `failures` 次 `getSessionCost` 抛错、之后返回 `cost`（模拟计量**持续**打嗝后恢复）。

    不碰 DB ⇒ 无需种 `session_token_usage` 行。

    **三个读法必须齐全**（照 `TokenUsageService` 公开面）：`buildRoutingContext` 会依次读
    `getSessionCost` / `getSessionTurnCount` / `getLastModelId`，缺一个就 `AttributeError`
    → 被那里的 `except` 吞成零上下文路由 —— 用例能过有一层「靠异常被吞」的偶然性，会
    掩盖真实接线问题（假替身保真度：memory `qa-system-fake-double-fidelity`）。

    `calls` / `failures_on` 记录调用序与抛错位置，供用例钉住「那次失败确实落在**闸门**上」
    —— 闸门只读 `getSessionCost` 就返回；路由上下文读 `getSessionCost` 后**紧跟**
    `getSessionTurnCount`（`research_agent_model_resolution.py:72-74`），且 `_runFrom`
    的相位边界闸门**先于**该相位的模型解析（`research_agent_service.py:636`）。
    """

    def __init__(self, *, failures: int, cost: Decimal = Decimal("0.01")) -> None:
        self._remaining = failures
        self._cost = cost
        self.calls: list[str] = []
        self.failures_on: list[int] = []

    async def getSessionCost(self, session: Any, sessionId: str) -> Decimal:
        self.calls.append("cost")
        if self._remaining > 0:
            self._remaining -= 1
            self.failures_on.append(len(self.calls) - 1)
            raise RuntimeError("计量持续打嗝")
        return self._cost

    async def getSessionTurnCount(self, session: Any, sessionId: str) -> int:
        self.calls.append("turns")
        return 0

    async def getLastModelId(self, session: Any, sessionId: str) -> str | None:
        self.calls.append("model")
        return None


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


def _parseFrames(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    events: list[str] = []
    payloads: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line]
        name = next((line[len("event: ") :] for line in lines if line.startswith("event: ")), None)
        if name is None:
            continue
        data = next((line[len("data: ") :] for line in lines if line.startswith("data: ")), None)
        events.append(name)
        payloads.append(json.loads(data) if data else {})
    return events, payloads


async def _runTurnCapturingStream(
    client: AsyncClient, headers: dict[str, str], sessionId: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """先订阅、再触发 turn，返回完整帧序（ASGI 缓冲 ⇒ 返回即流已关闭）。"""
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
    return _parseFrames(resp.text)


async def _sessionStatus(
    client: AsyncClient, headers: dict[str, str], sessionId: str
) -> str:
    detail = await client.get(f"{_BASE}/sessions/{sessionId}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()["session"]["status"]


# ---------------------------------------------------------------------------
# 1. 超预算 ⇒ 第一个相位边界即终止
# ---------------------------------------------------------------------------


async def test_over_budget_turn_terminates_at_first_phase_boundary(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    dbSession: AsyncSession,
) -> None:
    """成本 ≥ 预算 ⇒ 相位边界终止：`research.error{turn_failed}`、无 `done`、会话 `failed`。"""
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_INJECTED_BUDGET),
    )
    sid = await _createSession(client, authHeaders)
    await _seedUsage(dbSession, sid, _OVER_BUDGET_COST)

    events, payloads = await _runTurnCapturingStream(client, authHeaders, sid)

    assert events[0] == "research.connected"
    assert "research.done" not in events
    assert events[-1] == "research.error"
    error = payloads[-1]
    assert error["code"] == "turn_failed"
    # 消息带**真实**成本与预算（读得出来才有可操作性）
    assert "0.500000" in error["message"]
    assert "0.100000" in error["message"]
    assert "预算" in error["message"] and "成本" in error["message"]
    # 第一个相位（intent）尚未开跑 ⇒ 无任何相位事件（闸门在相位之前）
    assert "research.intent" not in events
    assert await _sessionStatus(client, authHeaders, sid) == "failed"


# ---------------------------------------------------------------------------
# 2. 不误伤：成本低于预算照常 done
# ---------------------------------------------------------------------------


async def test_under_budget_turn_reaches_done(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    dbSession: AsyncSession,
) -> None:
    """成本 < 预算 ⇒ 闸门不触发，会话正常 `done`（防误伤）。"""
    router = FakeModelRouter()
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_INJECTED_BUDGET, modelRouter=router),
    )
    sid = await _createSession(client, authHeaders)
    await _seedUsage(dbSession, sid, _UNDER_BUDGET_COST)

    events, _ = await _runTurnCapturingStream(client, authHeaders, sid)

    assert events[-1] == "research.done"
    assert "research.error" not in events
    assert await _sessionStatus(client, authHeaders, sid) == "done"
    # 控制组：本轮确实走到了模型路由（证明下面那条 `router.calls == []` 不是「没接上」）
    assert router.calls, "预算充足时路由应被调用（对照组）"


# ---------------------------------------------------------------------------
# 3. 改模型零发生（承重）：终止 ≠ 换模型
# ---------------------------------------------------------------------------


async def test_gate_path_never_consults_the_model_router(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    dbSession: AsyncSession,
) -> None:
    """闸门路径**不**触碰模型路由：终止与换模型正交。

    用户选定的模型一律照用（SSOT：memory `qa-system-no-model-override`）。路由器
    `ModelRouterService.selectModel` 的超预算**降级**分支在自动路由下会换成最便宜模型 ——
    本闸门**不得**走那条路：它在任何模型选择发生**之前**就终止本轮，故记录的调用数为 0。
    对照组见 `test_under_budget_turn_reaches_done`（同一接线、预算充足 ⇒ 路由被调用）。
    """
    router = FakeModelRouter()
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_INJECTED_BUDGET, modelRouter=router),
    )
    sid = await _createSession(client, authHeaders)
    await _seedUsage(dbSession, sid, _OVER_BUDGET_COST)

    events, _ = await _runTurnCapturingStream(client, authHeaders, sid)

    assert events[-1] == "research.error"  # 本轮确实终止了（非「什么都没发生」）
    assert router.calls == [], "闸门路径绝不允许选/换模型"


# ---------------------------------------------------------------------------
# 4. 读失败 ⇒ 降级可见（§3.2）：fail-open 不变，但发一条降级 `research.error`
# ---------------------------------------------------------------------------


async def test_repeated_cost_read_failures_emit_one_degraded_error_and_still_done(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计量**持续**读失败 ⇒ 恰好一条降级 `research.error`，且本轮仍正常 `done`。

    接线级证明：单测只证明判定逻辑，这里证明它真的挂进了相位边界、真的经 SSE 出帧、
    且**不**关流、**不**落 failed（`terminal=False` 的可执行表述）。
    """
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(
            sessionBudget=_INJECTED_BUDGET, tokenUsage=_FlakyTokenUsage(failures=99)
        ),
    )
    sid = await _createSession(client, authHeaders)

    events, payloads = await _runTurnCapturingStream(client, authHeaders, sid)

    assert events[-1] == "research.done", "降级不得关流/不得到此为止"
    assert events.count("research.error") == 1, "整轮（7 个相位边界）只发一条"
    idx = events.index("research.error")
    err = payloads[idx]
    assert err["code"] == "budget_metering_degraded"
    assert err["uiHint"] == "degraded"
    assert err["code"] not in TERMINAL_ERROR_CODES, "降级 event 不得进终态集合"
    assert err["message"], "用户可见文案不得为空"
    assert "research.intent" in events and idx > events.index("research.intent")
    assert idx < events.index("research.plan"), "N=2 ⇒ 第二个相位边界前发出"
    assert await _sessionStatus(client, authHeaders, sid) == "done"


async def test_single_cost_read_failure_is_silent(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**反向断言**：只失败一次（< 阈值）⇒ 无 `research.error`，照常 `done`。

    并钉住那次失败**确实落在闸门**（否则本用例证明不了「闸门单次读失败 ⇒ 静默」）：
    闸门的相位边界读（`_runFrom` 循环头）**先于**该相位的模型解析 ⇒ 首个
    `getSessionCost` 来自闸门；路由上下文那次读会**紧跟** `getSessionTurnCount`。
    """
    usage = _FlakyTokenUsage(failures=1)
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _service(sessionBudget=_INJECTED_BUDGET, tokenUsage=usage),
    )
    sid = await _createSession(client, authHeaders)

    events, _ = await _runTurnCapturingStream(client, authHeaders, sid)

    assert "research.error" not in events
    assert events[-1] == "research.done"
    assert await _sessionStatus(client, authHeaders, sid) == "done"
    # 对照组：路由上下文**确实**跑过（否则下面的顺序断言是空转 —— 没有第二个消费者）
    assert "turns" in usage.calls, "路由上下文未运行 ⇒ 顺序断言无意义"
    # 那次失败落在**第 0 次读**（闸门），且闸门的读**不**紧跟 turnCount 读
    assert usage.failures_on == [0], "唯一那次失败必须落在闸门（不是路由上下文）"
    assert usage.calls[:2] == ["cost", "cost"], (
        "首个 getSessionCost 之后不是 getSessionTurnCount ⇒ 它是闸门那次（非路由）"
    )
