"""Task 7 集成测试：编排运行的 steps 路径（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- stepNo 升序、afterStepNo 生效
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
    _seedStep,
)
from app.tests.integration._agent_runs_api_helpers import client as client

# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------


async def test_steps_are_ascending_and_honor_after_step_no(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    assert created.status_code == 202, created.text
    run = await _seedRun(
        dbSession,
        runId="44444444-4444-4444-4444-444444444444",
        modelConfigId=modelId,
        status="RUNNING",
    )
    runId = run.run_id
    for stepNo in (0, 1, 2):
        await _seedStep(dbSession, run, stepNo)

    resp = await client.get(f"{_BASE_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    assert resp.status_code == 200, resp.text
    assert [row["stepNo"] for row in resp.json()] == [0, 1, 2]

    after = await client.get(
        f"{_BASE_PATH}/{runId}/steps?afterStepNo=1", headers=OWNER_HEADERS
    )
    assert after.status_code == 200, after.text
    assert [row["stepNo"] for row in after.json()] == [1, 2]

    bounded = await client.get(
        f"{_BASE_PATH}/{runId}/steps?limit=5000", headers=OWNER_HEADERS
    )
    assert bounded.status_code == 200, bounded.text
    assert len(bounded.json()) == 3
