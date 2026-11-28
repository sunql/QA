"""I3-a 集成测试：采购 Copilot 入口（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 域前缀门槛：非 `PROCUREMENT_` Agent ⇒ 422（不是静默跑一个全局 Agent）；
- `mode=task`：202 + 任务引用，且引用指向的 run 真的可读（question = 用户消息）；
- `mode=stream`：SSE 首帧是 `copilot_run`（含 runId + 轨迹地址），
  且 run 已落库（不是「先吐事件后建任务」的假流）。
- 编排开关关闭 ⇒ 路由不存在（404），与 `/agent-runs` 同开关同生共死。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.services.procurement_copilot import CopilotStreamOptions, copilotStreamOptions
from app.tests import _pg_support
from app.tests.integration._agent_runs_api_helpers import (
    OUTSIDER_HEADERS,
    OWNER_HEADERS,
    _createAgent,
    _createModel,
    _createProfile,
    _createTool,
)

# 该夹具把 ENABLE_AGENT_ORCHESTRATION 打开后再建 app（fixture 必须显式导入）
from app.tests.integration._agent_runs_api_helpers import client as client

PROC_AGENT_CODE = "PROCUREMENT_TEST_AGENT"
COPILOT_PATH = "/api/v1/procurement/copilot/messages"


async def _seedRunnableProcurementAgent(client: AsyncClient) -> int:
    """PROCUREMENT_ 前缀的 Agent + 工具 + 启用 profile + active 模型。"""
    await _createAgent(client, PROC_AGENT_CODE)
    await _createTool(client)
    await _createProfile(client, agentCode=PROC_AGENT_CODE)
    return await _createModel(client)


def _message(agentCode: str, modelId: int, *, mode: str = "task") -> dict:
    return {
        "message": "哪些供应商能供不锈钢板？",
        "agentCode": agentCode,
        "modelConfigId": modelId,
        "mode": mode,
    }


async def test_non_procurement_agent_is_rejected_422(
    client: AsyncClient,
) -> None:
    """非采购域 Agent 一律 422 —— Copilot 不是通用 Agent 的旁路。"""
    await _createAgent(client)  # ORCH_RUN_AGENT（无前缀）
    await _createTool(client)
    await _createProfile(client)  # 它其实完全可运行
    modelId = await _createModel(client)

    resp = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_message("ORCH_RUN_AGENT", modelId),
    )

    assert resp.status_code == 422, resp.text
    assert "PROCUREMENT_" in resp.text


async def test_task_mode_returns_task_reference_and_run_is_readable(
    client: AsyncClient,
) -> None:
    """mode=task ⇒ 202 任务引用；引用里的 runId 真的能读回同一条问题。"""
    modelId = await _seedRunnableProcurementAgent(client)

    resp = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_message(PROC_AGENT_CODE, modelId),
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "QUEUED"
    runId = body["runId"]
    assert body["stepsUrl"] == f"/api/v1/agent-runs/{runId}/steps"

    detail = await client.get(f"/api/v1/agent-runs/{runId}", headers=OWNER_HEADERS)
    assert detail.status_code == 200, detail.text
    assert detail.json()["question"] == "哪些供应商能供不锈钢板？"
    assert detail.json()["agentCode"] == PROC_AGENT_CODE


async def test_stream_mode_first_event_is_run_reference(
    client: AsyncClient,
) -> None:
    """mode=stream ⇒ 首帧 copilot_run（runId + stepsUrl），事件先于步骤。

    转发参数压到 0.2s：本用例不跑 Worker（run 永远停在 QUEUED），靠**连接超时**
    结束流；这同时验证「超时只结束连接，不碰运行」的语义。
    """
    modelId = await _seedRunnableProcurementAgent(client)
    # ASGITransport 持有被测 app：覆盖转发参数为「20ms 轮询 / 0.2s 上限」
    testApp = client._transport.app  # noqa: SLF001 - 测试内取被测 app
    testApp.dependency_overrides[copilotStreamOptions] = lambda: CopilotStreamOptions(
        pollInterval=0.02, maxWaitSeconds=0.2
    )
    try:
        async with client.stream(
            "POST",
            COPILOT_PATH,
            headers=OWNER_HEADERS,
            json=_message(PROC_AGENT_CODE, modelId, mode="stream"),
        ) as response:
            assert response.status_code == 200, await response.aread()
            assert response.headers["content-type"].startswith("text/event-stream")
            body = await response.aread()
    finally:
        testApp.dependency_overrides.pop(copilotStreamOptions, None)

    text = body.decode()
    assert text.startswith("event: copilot_run")
    assert '"runId"' in text
    assert "/api/v1/agent-runs/" in text
    # 超时以事件收尾（不是 500、不是无限挂起）
    assert "event: copilot_error" in text
    assert '"errorType": "timeout"' in text


@pytest.fixture
async def orchestrationDisabledClient(monkeypatch):
    """编排开关关闭的 API client（同一真库，另一套 wiring）。"""
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        getSettings.cache_clear()


async def test_copilot_route_absent_when_orchestration_disabled(
    orchestrationDisabledClient: AsyncClient,
) -> None:
    """开关关闭 ⇒ 404：这个面整体不存在，而不是「存在但报错」。"""
    resp = await orchestrationDisabledClient.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json={"message": "x", "agentCode": PROC_AGENT_CODE, "modelConfigId": 1},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 多轮会话（feat/copilot-multiturn）
# ---------------------------------------------------------------------------


async def test_first_message_creates_conversation_and_turn(
    client: AsyncClient,
) -> None:
    modelId = await _seedRunnableProcurementAgent(client)

    resp = await client.post(
        COPILOT_PATH, headers=OWNER_HEADERS, json=_message(PROC_AGENT_CODE, modelId)
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["conversationId"]
    assert body["resumed"] is False

    detail = await client.get(
        f"/api/v1/procurement/copilot/conversations/{body['conversationId']}",
        headers=OWNER_HEADERS,
    )
    assert detail.status_code == 200, detail.text
    turns = detail.json()["turns"]
    assert len(turns) == 1
    assert turns[0]["question"] == "哪些供应商能供不锈钢板？"
    assert turns[0]["runId"] == body["runId"]


async def test_second_message_carries_context_into_new_run(
    client: AsyncClient,
) -> None:
    """同一会话的**不同**追问 ⇒ 新 run，且 run.question 带上一轮上下文。

    同文字重复提问不在此列：run 级同日复用会直接指向既有 run
    （见 test_same_day_same_question_reuses_run，2026-11-24 产品口径）。
    """
    modelId = await _seedRunnableProcurementAgent(client)
    first = (
        await client.post(
            COPILOT_PATH, headers=OWNER_HEADERS, json=_message(PROC_AGENT_CODE, modelId)
        )
    ).json()

    followup = "那B125的情况呢？"
    second = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json={**_message(PROC_AGENT_CODE, modelId), "message": followup,
              "conversationId": first["conversationId"]},
    )

    assert second.status_code == 202, second.text
    body = second.json()
    assert body["runId"] != first["runId"]
    assert body["conversationId"] == first["conversationId"]
    assert body["resumed"] is False

    run = await client.get(f"/api/v1/agent-runs/{body['runId']}", headers=OWNER_HEADERS)
    question = run.json()["question"]
    assert "[对话上下文]" in question
    assert "哪些供应商能供不锈钢板？" in question  # 上一轮进上下文
    assert question.rstrip().endswith(followup)  # 当前追问在最后

    conversation = await client.get(
        f"/api/v1/procurement/copilot/conversations/{body['conversationId']}",
        headers=OWNER_HEADERS,
    )
    assert len(conversation.json()["turns"]) == 2


async def test_paused_need_input_is_resumed_not_restarted(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """上一轮停在「等我回答」⇒ 下一句 resume 同一个 run（不重复花钱重跑）。"""
    from sqlalchemy import update

    from app.domain.agent_orchestration_models import AgentOrchestrationRun

    modelId = await _seedRunnableProcurementAgent(client)
    first = (
        await client.post(
            COPILOT_PATH, headers=OWNER_HEADERS, json=_message(PROC_AGENT_CODE, modelId)
        )
    ).json()

    await dbSession.execute(
        update(AgentOrchestrationRun)
        .where(AgentOrchestrationRun.run_id == first["runId"])
        .values(status="PAUSED", pause_code="NEEDS_INPUT")
    )
    await dbSession.commit()

    second = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json={
            **_message(PROC_AGENT_CODE, modelId),
            "conversationId": first["conversationId"],
            "message": "选 ITM-1 那个规格",
        },
    )

    assert second.status_code == 202, second.text
    body = second.json()
    assert body["resumed"] is True
    assert body["runId"] == first["runId"]
    assert body["status"] == "QUEUED"

    run = await client.get(f"/api/v1/agent-runs/{first['runId']}", headers=OWNER_HEADERS)
    runBody = run.json()
    assert runBody["status"] == "QUEUED"
    assert runBody["pauseCode"] is None
    assert "选 ITM-1 那个规格" in runBody["question"]


async def test_other_user_cannot_read_conversation(
    client: AsyncClient,
) -> None:
    modelId = await _seedRunnableProcurementAgent(client)
    created = (
        await client.post(
            COPILOT_PATH, headers=OWNER_HEADERS, json=_message(PROC_AGENT_CODE, modelId)
        )
    ).json()

    resp = await client.get(
        f"/api/v1/procurement/copilot/conversations/{created['conversationId']}",
        headers={"X-User-Id": "someone-else", "X-User-Roles": "analyst"},
    )

    assert resp.status_code == 404, resp.text


async def test_switching_agent_inside_conversation_is_rejected(
    client: AsyncClient,
) -> None:
    """会话绑定 Agent：换场景必须新开会话（否则上下文串味）。"""
    modelId = await _seedRunnableProcurementAgent(client)
    created = (
        await client.post(
            COPILOT_PATH, headers=OWNER_HEADERS, json=_message(PROC_AGENT_CODE, modelId)
        )
    ).json()

    resp = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json={
            **_message("PROCUREMENT_ANOTHER_AGENT", modelId),
            "conversationId": created["conversationId"],
        },
    )

    assert resp.status_code == 422, resp.text
    assert "新会话" in resp.text


LIST_PATH = "/api/v1/procurement/copilot/conversations"


async def test_conversation_list_returns_owner_thread_with_preview(
    client: AsyncClient,
) -> None:
    """会话列表：返回自己的会话 + 轮数 + 最后一问（侧栏「历史会话」用）。"""
    modelId = await _seedRunnableProcurementAgent(client)
    created = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_message(PROC_AGENT_CODE, modelId),
    )
    assert created.status_code == 202
    conversationId = created.json()["conversationId"]

    resp = await client.get(LIST_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    row = next(r for r in rows if r["conversationId"] == conversationId)
    assert row["agentCode"] == PROC_AGENT_CODE
    assert row["turnCount"] == 1
    assert row["lastQuestion"] == "哪些供应商能供不锈钢板？"
    assert row["updatedAt"]


async def test_conversation_list_is_owner_scoped(client: AsyncClient) -> None:
    """别人的会话不出现在我的列表里（与详情接口同为 404 口径）。"""
    modelId = await _seedRunnableProcurementAgent(client)
    created = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_message(PROC_AGENT_CODE, modelId),
    )
    assert created.status_code == 202
    conversationId = created.json()["conversationId"]

    resp = await client.get(LIST_PATH, headers=OUTSIDER_HEADERS)
    assert resp.status_code == 200
    ids = [r["conversationId"] for r in resp.json()]
    assert conversationId not in ids


async def test_delete_conversation_removes_it_from_list(client: AsyncClient) -> None:
    """删除自己的会话：204，随后列表与详情都不再可见。"""
    modelId = await _seedRunnableProcurementAgent(client)
    created = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_message(PROC_AGENT_CODE, modelId),
    )
    conversationId = created.json()["conversationId"]

    resp = await client.delete(f"{LIST_PATH}/{conversationId}", headers=OWNER_HEADERS)
    assert resp.status_code == 204

    rows = (await client.get(LIST_PATH, headers=OWNER_HEADERS)).json()
    assert conversationId not in [r["conversationId"] for r in rows]
    detail = await client.get(f"{LIST_PATH}/{conversationId}", headers=OWNER_HEADERS)
    assert detail.status_code == 404


async def test_delete_conversation_is_owner_scoped(client: AsyncClient) -> None:
    """别人的会话：删除返回 404，且原会话不受影响。"""
    modelId = await _seedRunnableProcurementAgent(client)
    created = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_message(PROC_AGENT_CODE, modelId),
    )
    conversationId = created.json()["conversationId"]

    blocked = await client.delete(
        f"{LIST_PATH}/{conversationId}", headers=OUTSIDER_HEADERS
    )
    assert blocked.status_code == 404

    rows = (await client.get(LIST_PATH, headers=OWNER_HEADERS)).json()
    assert conversationId in [r["conversationId"] for r in rows]


@pytest.mark.asyncio
async def test_same_day_same_question_reuses_run(client: AsyncClient) -> None:
    """同日同问复用（2026-11-24 需求）：同问两次 ⇒ 第二次复用首个 run，零新查询。"""
    modelId = await _seedRunnableProcurementAgent(client)
    payload = _message(PROC_AGENT_CODE, modelId)

    first = await client.post(COPILOT_PATH, headers=OWNER_HEADERS, json=payload)
    assert first.status_code == 202
    firstRunId = first.json()["runId"]

    second = await client.post(COPILOT_PATH, headers=OWNER_HEADERS, json=payload)
    assert second.status_code == 202
    assert second.json()["runId"] == firstRunId, "同日同问必须复用既有 run"

    # 不同问题 ⇒ 新 run（不复用）
    other = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json={**payload, "message": "换个问题：B125 的欠交情况如何？"},
    )
    assert other.status_code == 202
    assert other.json()["runId"] != firstRunId


# ---------------------------------------------------------------------------
# 场景槽位范围校验（2026-11-28 生产缺陷：周期期序填 1 ⇒ 查了所有供应商）
# ---------------------------------------------------------------------------

_DELIVERY_SCENARIO_CODE = "delivery_tracking"
_DELIVERY_AGENT_CODE = "DELIVERY_TRACKING_AGENT"

#: 到货跟踪场景槽位（与 seed_copilot_scenarios.py 同构的关键部分）。
_DELIVERY_SLOT_SCHEMA: list[dict] = [
    {
        "key": "monthsBack",
        "type": "number",
        "label": "时间窗",
        "minimum": 1,
        "maximum": 36,
    },
    {
        "key": "anchorDay",
        "type": "number",
        "label": "月度周期锚点日",
        "minimum": 1,
        "maximum": 28,
    },
    {
        "key": "periodIndex",
        "type": "number",
        "label": "周期期序",
        "minimum": -12,
        "maximum": 0,
        "hint": "0=本期，-1=上期",
    },
    {"key": "supplierKeys", "type": "entityMulti", "label": "供应商"},
]

#: **已部署库的旧形态**：槽位在，但没有 minimum / maximum（seed 不覆盖已
#: 存在场景行 ⇒ 新增字段拿不到）。配置层读不到范围 ⇒ 必须靠契约层兜住。
_DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE: list[dict] = [
    {"key": "anchorDay", "type": "number", "label": "月度周期锚点日"},
    {"key": "periodIndex", "type": "number", "label": "周期期序"},
    {"key": "supplierKeys", "type": "entityMulti", "label": "供应商"},
]


async def _insertDeliveryScenario(slotSchema: list[dict]) -> None:
    """插一条到货跟踪场景配置（真实 PG）。"""
    from app.domain.copilot_scenario_models import CopilotScenario
    from app.infrastructure import database as dbModule

    factory = dbModule.getSessionFactory()
    async with factory() as session:
        session.add(
            CopilotScenario(
                scenario_code=_DELIVERY_SCENARIO_CODE,
                name="到货跟踪",
                description="测试用场景",
                icon_code="car",
                agent_code=_DELIVERY_AGENT_CODE,
                slot_schema=slotSchema,
                question_template="分析订单到货情况",
                chips=[],
                result_renderer="delivery_tracking",
                required_role=None,
                sort_order=30,
                enabled=True,
            )
        )
        await session.commit()


async def _countOrchestrationRuns() -> int:
    """编排 run 行数（用于断言「非法请求不产生 run」）。"""
    from sqlalchemy import func, select

    from app.domain.agent_orchestration_models import AgentOrchestrationRun
    from app.infrastructure import database as dbModule

    factory = dbModule.getSessionFactory()
    async with factory() as session:
        rows = await session.execute(select(func.count()).select_from(AgentOrchestrationRun))
        return int(rows.scalar() or 0)


def _scenarioMessage(*, slots: dict) -> dict:
    return {
        "message": "看本期到货情况",
        "agentCode": _DELIVERY_AGENT_CODE,
        "modelConfigId": 1,
        "scenarioCode": _DELIVERY_SCENARIO_CODE,
        "slots": slots,
        "mode": "task",
    }


@pytest.mark.asyncio
async def test_out_of_range_period_index_is_422_and_creates_no_run(
    client: AsyncClient,
) -> None:
    """周期期序填 1（正数非法）⇒ 422，**不创建 run**（不进 LLM）。

    修复前：工具侧报错 → runner 记 FAILED 但不终止 run → LLM 重规划 →
    丢了 supplierKeys ⇒ 查了所有供应商（生产错结果）。
    """
    await _insertDeliveryScenario(_DELIVERY_SLOT_SCHEMA)
    before = await _countOrchestrationRuns()

    resp = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_scenarioMessage(slots={"periodIndex": 1, "supplierKeys": ["B109"]}),
    )

    assert resp.status_code == 422, resp.text
    assert "周期期序" in resp.text
    assert await _countOrchestrationRuns() == before, "非法槽位不得创建编排 run"


@pytest.mark.asyncio
async def test_period_index_without_anchor_day_is_422(client: AsyncClient) -> None:
    """给了周期期序却没给锚点日 ⇒ 422（工具同款规则提前到发起前）。"""
    await _insertDeliveryScenario(_DELIVERY_SLOT_SCHEMA)

    resp = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_scenarioMessage(slots={"periodIndex": 0, "supplierKeys": ["B109"]}),
    )

    assert resp.status_code == 422, resp.text
    assert "周期期序" in resp.text


@pytest.mark.asyncio
async def test_positive_period_index_is_422_even_without_configured_range(
    client: AsyncClient,
) -> None:
    """**DB 里没有 min/max 时**，周期期序填 1 仍被 422 拦下。

    已部署库的存量场景行拿不到 seed 新增的 minimum/maximum（seed 不覆盖
    已存在行）⇒ 配置层读不到范围 ⇒ 靠**契约层**（periodIndex ≤ 0，真值在
    代码里）兜住 ⇒ 生产防线不依赖 seed 补丁是否跑过。
    """
    await _insertDeliveryScenario(_DELIVERY_SLOT_SCHEMA_WITHOUT_RANGE)
    before = await _countOrchestrationRuns()

    resp = await client.post(
        COPILOT_PATH,
        headers=OWNER_HEADERS,
        json=_scenarioMessage(slots={"periodIndex": 1, "supplierKeys": ["B109"]}),
    )

    assert resp.status_code == 422, resp.text
    assert "周期期序" in resp.text
    assert await _countOrchestrationRuns() == before, "非法槽位不得创建编排 run"
