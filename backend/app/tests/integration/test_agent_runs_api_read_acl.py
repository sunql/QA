"""Task 7 集成测试：编排运行的读 ACL（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 他人的 run 一律 404（get/steps/resume/cancel 同一文案），admin 可读
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.integration._agent_runs_api_helpers import (
    _BASE_PATH,
    ADMIN_HEADERS,
    OUTSIDER_HEADERS,
    OWNER_HEADERS,
    _createAgent,
    _createModel,
    _createProfile,
    _createRun,
    _createTool,
    _seedRun,
    _seedStep,
)
from app.tests.integration._agent_runs_api_helpers import client as client

# ---------------------------------------------------------------------------
# 读 ACL
# ---------------------------------------------------------------------------


async def test_other_user_cannot_read_or_mutate_run(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="22222222-2222-2222-2222-222222222222",
        modelConfigId=modelId,
        status="PAUSED",
    )
    runId = run.run_id
    await _seedStep(dbSession, run, 0)

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=OUTSIDER_HEADERS)
    steps = await client.get(f"{_BASE_PATH}/{runId}/steps", headers=OUTSIDER_HEADERS)
    resumed = await client.post(
        f"{_BASE_PATH}/{runId}/resume", headers=OUTSIDER_HEADERS, json={}
    )
    cancelled = await client.post(
        f"{_BASE_PATH}/{runId}/cancel", headers=OUTSIDER_HEADERS, json={}
    )

    assert detail.status_code == 404, detail.text
    assert steps.status_code == 404, steps.text
    assert resumed.status_code == 404, resumed.text
    assert cancelled.status_code == 404, cancelled.text
    # 404 文案统一，不泄露 run 是否存在
    assert detail.json()["error"] == "agent run not found"
    assert detail.json()["error"] == steps.json()["error"]
    assert detail.json()["error"] == resumed.json()["error"]
    assert detail.json()["error"] == cancelled.json()["error"]


async def test_admin_can_read_any_run(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=ADMIN_HEADERS)
    assert detail.status_code == 200, detail.text
    assert detail.json()["runId"] == runId

    steps = await client.get(f"{_BASE_PATH}/{runId}/steps", headers=ADMIN_HEADERS)
    assert steps.status_code == 200, steps.text
    assert steps.json() == []


async def test_unknown_run_id_returns_404(client: AsyncClient) -> None:
    resp = await client.get(
        f"{_BASE_PATH}/33333333-3333-3333-3333-333333333333", headers=OWNER_HEADERS
    )
    assert resp.status_code == 404, resp.text
    # 统一文案：与「别人的 run」完全一致，不泄露 run 是否存在
    assert resp.json()["error"] == "agent run not found"
