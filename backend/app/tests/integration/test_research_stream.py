"""研究进度 SSE 端点（Task 8）：独立端点 + 进程内事件总线（真实 PG + 完整 HTTP 链路）。

覆盖契约：
- 首事件 `research.connected`；§4.5 事件族按序流动（含 Task 5 MEDIUM-7 的
  `research.step.sql / step.data / step.chart` 三件套与 step.done 的生命周期顺序）；
- `research.checkpoint` 事件与「暂停不关流」（客户端决策期间流保持打开）；
- 越权 404 / 匿名 401（与 REST 端点同口径，不泄露存在性）；
- 终态关流：`research.done`、**终态**错误（`turn_failed`）；
- 降级类错误（`llm_unavailable`）**不关流**（其载荷 `terminal=False`；流端按 code 的终态标记
  决定是否关流，见 task-8-report 偏差 #1）。但「其后状态机仍推进到 done」只是**常见**情形、
  **不是**保证 —— Task 17 起：无可用 LLM 且计划回落到**无 SQL 的单步** ⇒ 后续
  `research.error{turn_failed}` 才是终态（见
  `test_degraded_error_does_not_close_stream` 的限定说明）。若在降级错误处关流，会让客户端丢掉
  后续全部事件（Task 5 MEDIUM-4 的可见降级信号与 §4.5 的错误语义共用同名事件）。

驱动方式：httpx 0.27 的 `ASGITransport` 把整个响应体缓冲到 `more_body=False`
（`ASGIResponseStream`），故 `client.stream` / `client.get` 在流关闭前不返回。测试因此
**并发**发起 GET 流与 POST turn：先订阅（`create_task` + 等总线订阅数 > 0），再触发
turn（Brief 契约：先订阅、不做历史回放）。

后台状态机用 fake 管线（确定性、零网络、零 LLM），但**仍走真实 HTTP + 真实 PG +
真实 ResearchSessionService / ReportPlanner**（非假绿）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
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
    RaisingReporter,
    ScriptedLlmClient,
)

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"


async def _seedDatasource(
    dbSession: AsyncSession, name: str = "stream-test-oracle", *, isDefault: bool = True
) -> DataSource:
    """种一个**启用的业务数据源**（Task 13e：建会话必须能解析到业务源）。

    与 `test_research_api.py` 同形：Oracle 形态与生产（`THBI Oracle`）同口径；
    本套件的 runner 是 fake，适配器只被构造、从不建连，故 host 用不可达假名即可。
    """
    ds = DataSource(
        name=name,
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=isDefault,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    """用户 A 的 stub 头（DB 命中 → dbUserId 非空）+ 一个启用的默认业务源。

    Task 13e 起 `POST /research/sessions` 是 fail-closed：解析不到启用的业务源即 422，
    故建会话的夹具必须同步种源（与 test_research_api 同款）。
    """
    dbSession.add(
        User(username="research-stream-a", display_name="a", email=None, enabled=True,
             password_hash=None)
    )
    await dbSession.commit()
    await _seedDatasource(dbSession)
    return {"X-User-Id": "research-stream-a"}


@pytest.fixture()
async def secondUserHeaders(dbSession: AsyncSession) -> dict[str, str]:
    """用户 B 的 stub 头（越权 404 断言用）+ 一个非默认业务源。

    用户 B 只用于「流他人会话 ⇒ 404」；其源不参与建会话的默认源解析，
    故用 `isDefault=False` 避免与 `authHeaders` 的默认源冲突（同 `test_research_api.py:556`）。
    """
    dbSession.add(
        User(username="research-stream-b", display_name="b", email=None, enabled=True,
             password_hash=None)
    )
    await dbSession.commit()
    await _seedDatasource(dbSession, "stream-test-secondary", isDefault=False)
    return {"X-User-Id": "research-stream-b"}


#: §4.5 事件族在 autoConfirm 全跑路径上的顺序（子序列断言，不含 connected/checkpoint）。
_FULL_RUN_ORDER = (
    "research.intent",
    "research.esl",
    "research.plan",
    "research.step.start",
    "research.step.sql",
    "research.step.data",
    "research.step.chart",
    "research.step.done",
    "research.hypothesis",
    "research.finding",
    "research.report",
    "research.done",
)


# ---------------------------------------------------------------------------
# 替身管线（复用 Task 5/6 的 fake；报告阶段走真实 ReportPlanner）
# ---------------------------------------------------------------------------


def _fullService(**kwargs: Any) -> ResearchAgentService:
    """全跑管线（autoConfirm）：intent → esl → plan → execute → … → report → done。"""
    scripted = ScriptedLlmClient(HYPOTHESIS_JSON, "解读：收货量下降明显。")
    return ResearchAgentService(
        esl=kwargs.get("esl", FakeEsl()),
        sessionService=ResearchSessionService(),
        planner=kwargs.get("planner", FakePlanner()),
        runner=kwargs.get("runner", FakeRunner()),
        chartService=kwargs.get("chartService", FakeChart()),
        llmFactory=kwargs.get("llmFactory", lambda cfg: scripted),
        usageRecorder=kwargs.get("usageRecorder", FakeUsageRecorder()),
        reporter=kwargs.get("reporter"),  # None → 真实 ReportPlanner（产品默认路径）
        modelConfigs=kwargs.get("modelConfigs", FakeModelConfigs()),
        modelRouter=kwargs.get("modelRouter", FakeModelRouter()),
        ontology=kwargs.get("ontology", FakeOntology()),
        autoConfirm=True,
    )


async def _createSession(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(f"{_BASE}/sessions", json={"question": QUESTION}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _waitForSubscriber(sessionId: str, timeout: float = 5.0) -> None:
    """等流端完成订阅（先订阅、再触发 turn 的时序保证）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while bus.subscriberCount(sessionId) == 0:
        if loop.time() > deadline:
            raise AssertionError("SSE 流未在超时内完成订阅")
        await asyncio.sleep(0.01)


async def _readStream(
    client: AsyncClient, sessionId: str, headers: dict[str, str]
) -> tuple[list[str], list[dict[str, Any]]]:
    """GET 流并读完整帧序列（ASGI 缓冲 ⇒ 返回即流已关闭）。"""
    resp = await client.get(f"{_BASE}/stream", params={"sessionId": sessionId}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _parseFrames(resp.text)


def _parseFrames(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """解析 SSE 帧；`: ping` 心跳行（无 event 字段）忽略。"""
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


def _assertSubsequence(events: list[str], expected: tuple[str, ...]) -> None:
    """expected 必须是 events 的**顺序子序列**（允许中间夹杂 checkpoint / error 等）。"""
    position = 0
    for name in events:
        if position < len(expected) and name == expected[position]:
            position += 1
    assert position == len(expected), f"事件顺序缺失: {expected[position:]} in {events}"


# ---------------------------------------------------------------------------
# 事件族与生命周期
# ---------------------------------------------------------------------------


async def test_stream_emits_connected_and_full_stage_events(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """首事件 connected + §4.5 全事件族（含 step.* 三件套）按序流动，done 收尾。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _createSession(client, authHeaders)

    stream = asyncio.create_task(_readStream(client, sid, authHeaders))
    await _waitForSubscriber(sid)
    accepted = await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": QUESTION}, headers=authHeaders
    )
    assert accepted.status_code == 202, accepted.text

    events, payloads = await asyncio.wait_for(stream, timeout=30)
    byEvent = dict(zip(events, payloads, strict=True))

    assert events[0] == "research.connected"
    assert byEvent["research.connected"] == {"sessionId": sid}
    assert events[-1] == "research.done"
    _assertSubsequence(events, _FULL_RUN_ORDER)
    # step 三件套的载荷契约（设计 §4.5）
    assert byEvent["research.step.sql"]["sql"] == "SELECT 1"
    assert byEvent["research.step.data"]["data"] == [{"month": "2026-01", "cnt": 100}]
    assert byEvent["research.step.data"]["rowCount"] == 1
    assert "chartType" in byEvent["research.step.chart"]
    assert byEvent["research.done"]["degraded"] is False
    # 流关闭后订阅已清理（无泄漏）
    assert bus.subscriberCount(sid) == 0


async def test_stream_emits_checkpoint_event_and_survives_pause(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """空 scope → 固定 #1 暂停：发 `research.checkpoint` 且**不关流**；决策后续跑到 done。

    `emptyOnce=True` 的 ESL：首次三臂全空触发暂停，恢复时重跑 ESL 得到非空结果。
    服务实例在两个后台任务间共享（同一 ESL 计数器），故工厂返回同一实例。
    """
    service = _fullService(esl=FakeEsl(emptyOnce=True))
    monkeypatch.setattr(researchModule, "buildResearchAgentService", lambda: service)
    sid = await _createSession(client, authHeaders)

    stream = asyncio.create_task(_readStream(client, sid, authHeaders))
    await _waitForSubscriber(sid)
    await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": QUESTION}, headers=authHeaders
    )

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    checkpoint = detail.json()["pendingCheckpoint"]
    assert checkpoint is not None and checkpoint["options"]["signal"] == "empty_scope"

    answered = await client.post(
        f"{_BASE}/checkpoints/{checkpoint['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert answered.status_code == 200, answered.text

    events, payloads = await asyncio.wait_for(stream, timeout=30)
    byEvent = dict(zip(events, payloads, strict=True))
    assert events[0] == "research.connected"
    assert byEvent["research.checkpoint"]["checkpointId"] == checkpoint["id"]
    assert byEvent["research.checkpoint"]["phase"] == "intent"
    assert events[-1] == "research.done"
    _assertSubsequence(events, _FULL_RUN_ORDER)


# ---------------------------------------------------------------------------
# 鉴权 / 越权
# ---------------------------------------------------------------------------


async def test_stream_requires_auth(client: AsyncClient) -> None:
    """无身份头 → 401（流端点与 REST 同口径）。"""
    resp = await client.get(f"{_BASE}/stream", params={"sessionId": str(uuid.uuid4())})
    assert resp.status_code == 401


async def test_stream_404_for_other_user_session(
    client: AsyncClient,
    authHeaders: dict[str, str],
    secondUserHeaders: dict[str, str],
) -> None:
    """他人会话的流一律 404（不泄露存在性），且不建立订阅。"""
    sid = await _createSession(client, authHeaders)
    resp = await client.get(f"{_BASE}/stream", params={"sessionId": sid}, headers=secondUserHeaders)
    assert resp.status_code == 404
    assert bus.subscriberCount(sid) == 0


# ---------------------------------------------------------------------------
# 终态：关流与不关流
# ---------------------------------------------------------------------------


async def test_stream_closes_after_terminal_error(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告装配抛错 → turn 失败：发 `research.error{turn_failed}` 后关流。"""
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _fullService(reporter=RaisingReporter()),
    )
    sid = await _createSession(client, authHeaders)

    stream = asyncio.create_task(_readStream(client, sid, authHeaders))
    await _waitForSubscriber(sid)
    await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": QUESTION}, headers=authHeaders
    )

    events, payloads = await asyncio.wait_for(stream, timeout=30)
    assert events[0] == "research.connected"
    assert events[-1] == "research.error"
    assert payloads[-1]["code"] == "turn_failed"
    assert "boom" in payloads[-1]["message"]
    assert "research.done" not in events  # 终态错误不补 done


async def test_degraded_error_does_not_close_stream(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """无可用 LLM（llm_unavailable）**在计划步自带 SQL 时**是降级而非终态：流继续到 done。

    限定条件（Task 17 起）：本结论**仅**在该轮计划步**自带 SQL**（无需 LLM 生成）时成立 ——
    这里的 `FakePlanner` 默认步带 `sql="SELECT 1"`，故无 LLM 仍能执行、走到 `research.done`。
    若计划回落到**无 SQL 的单步**（`plan=None` ⇒ `singleStepPlan`，步 `sql: None`），同一「无 LLM」
    输入会因该步拿不到 SQL 而得 `STEP_MISSING_SQL` ⇒ 全部步失败 ⇒ 按 N1' 急停为**终态**
    （`research.error{turn_failed}` + 会话 failed，不出 report/done）—— 见用户裁定与
    `test_research_agent_service.py::test_no_llm_single_step_fallback_fails_turn_without_report`。
    """
    monkeypatch.setattr(
        researchModule,
        "buildResearchAgentService",
        lambda: _fullService(llmFactory=None, modelConfigs=FakeModelConfigs([])),
    )
    sid = await _createSession(client, authHeaders)

    stream = asyncio.create_task(_readStream(client, sid, authHeaders))
    await _waitForSubscriber(sid)
    await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": QUESTION}, headers=authHeaders
    )

    events, payloads = await asyncio.wait_for(stream, timeout=30)
    codes = [p["code"] for e, p in zip(events, payloads, strict=True) if e == "research.error"]
    assert codes and set(codes) == {"llm_unavailable"}
    assert events[-1] == "research.done"
    assert payloads[-1]["degraded"] is True
    # Task 8.5：llm_unavailable 是**降级**（terminal=False）。本场景（步自带 SQL）下 ⇒ 会话不落
    # failed、继续到 done；但这不是普遍保证 —— 无 SQL 回落步时终态失败由 turn_failed 承载（见 docstring）。
    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    assert detail.json()["session"]["status"] == "done"


async def test_turn_wrapper_emits_terminal_error_on_pre_guard_failure(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """空白问题在 `_guardedRun` 之前抛 ⇒ 后台 wrapper 补发 `research.error` 并**终止**流。

    Task 8 fix round 1（Important #2）：`requireQuestion` 在 `_guardedRun` 之前抛
    ValueError，逃到 `_runTurnInBackground` 的 except。旧实现只 log+rollback、不发任何
    事件 ⇒ 流只收心跳永不终止。断言终止行为：`wait_for` 成功返回（生成器关闭）即证明
    流已终止，且错误是**末帧**、不补 done。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid = await _createSession(client, authHeaders)

    stream = asyncio.create_task(_readStream(client, sid, authHeaders))
    await _waitForSubscriber(sid)
    accepted = await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": "   "}, headers=authHeaders
    )
    assert accepted.status_code == 202, accepted.text

    events, payloads = await asyncio.wait_for(stream, timeout=30)
    assert events[0] == "research.connected"
    assert events[-1] == "research.error"
    assert payloads[-1]["code"] == "turn_failed"
    assert "研究问题不能为空" in payloads[-1]["message"]
    assert "research.done" not in events


async def test_resume_wrapper_emits_terminal_error_on_pre_guard_failure(
    client: AsyncClient, authHeaders: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """恢复路径在 `_guardedRun` 之前抛 ⇒ 后台 wrapper 补发 `research.error` 并**终止**流。

    走真实 `resumeTurn` 的 `resolveCheckpoint` 失败面（Task 7.5 rowcount==0 路径）：
    空 scope 先暂停出一个 pending checkpoint，再把 service 的 `resolveCheckpoint` 替换
    为抛 ValueError（注入点与真实「已决策」路径同源——两条真实触发路径「非法 action /
    已决策」都被 HTTP 层 Pydantic/路由校验挡在 `_resumeTurnInBackground` 之外，见报告）。
    """
    service = _fullService(esl=FakeEsl(empty=True))

    async def failingResolve(session, *, checkpointId, status, userChoice, userId=None):
        raise ValueError(f"checkpoint 非 pending（当前 confirmed）: {checkpointId}")

    service._sessions.resolveCheckpoint = failingResolve
    monkeypatch.setattr(researchModule, "buildResearchAgentService", lambda: service)
    sid = await _createSession(client, authHeaders)

    stream = asyncio.create_task(_readStream(client, sid, authHeaders))
    await _waitForSubscriber(sid)
    await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": QUESTION}, headers=authHeaders
    )

    detail = await client.get(f"{_BASE}/sessions/{sid}", headers=authHeaders)
    checkpoint = detail.json()["pendingCheckpoint"]
    assert checkpoint is not None and checkpoint["options"]["signal"] == "empty_scope"

    answered = await client.post(
        f"{_BASE}/checkpoints/{checkpoint['id']}/answer",
        json={"action": "confirm", "choice": {}},
        headers=authHeaders,
    )
    assert answered.status_code == 200, answered.text

    events, payloads = await asyncio.wait_for(stream, timeout=30)
    assert events[0] == "research.connected"
    assert events[-1] == "research.error"
    assert payloads[-1]["code"] == "turn_failed"
    assert "非 pending" in payloads[-1]["message"]
    assert "research.done" not in events
