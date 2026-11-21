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

from app.config import getSettings
from app.services.procurement_copilot import CopilotStreamOptions, copilotStreamOptions
from app.tests import _pg_support
from app.tests.integration._agent_runs_api_helpers import (
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
