"""Task 7 集成测试：编排运行的 cancel 路径（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- QUEUED/PAUSED → CANCELLED；RUNNING → cancelRequested=true；终态 → 409
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.integration._agent_runs_api_helpers import (
    _BASE_PATH,
    OWNER_HEADERS,
    _createAgent,
    _createModel,
    _createProfile,
    _createRun,
    _createTool,
    _seedRun,
)
from app.tests.integration._agent_runs_api_helpers import client as client

# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_queued_run_becomes_cancelled(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    resp = await client.post(f"{_BASE_PATH}/{runId}/cancel", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["finishedAt"] is not None


async def test_cancel_paused_run_becomes_cancelled(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="cccccccc-cccc-cccc-cccc-cccccccccccc",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="NEEDS_INPUT",
    )
    runId = run.run_id

    resp = await client.post(f"{_BASE_PATH}/{runId}/cancel", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["finishedAt"] is not None


async def test_cancel_running_run_only_requests_cancel(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="dddddddd-dddd-dddd-dddd-dddddddddddd",
        modelConfigId=modelId,
        status="RUNNING",
    )
    runId = run.run_id

    resp = await client.post(f"{_BASE_PATH}/{runId}/cancel", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert body["cancelRequested"] is True
    assert body["finishedAt"] is None


async def test_cancel_terminal_run_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        modelConfigId=modelId,
        status="SUCCEEDED",
    )
    runId = run.run_id

    resp = await client.post(f"{_BASE_PATH}/{runId}/cancel", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 409, resp.text
