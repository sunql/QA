"""Task 7 集成测试：编排运行的 resume 路径（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 仅 PAUSED；NEEDS_INPUT/AMBIGUOUS 需补充输入；BUDGET_EXHAUSTED 需对应增量；
  PERMISSION_DENIED 复查当前 ACL；成功后清空暂停字段、模型 id 不变
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.integration._agent_runs_api_helpers import (
    _AGENT_CODE,
    _BASE_PATH,
    ADMIN_HEADERS,
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
# resume
# ---------------------------------------------------------------------------


async def test_resume_on_non_paused_run_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    resp = await client.post(f"{_BASE_PATH}/{runId}/resume", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 409, resp.text


async def test_resume_needs_input_requires_supplementary_input(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="55555555-5555-5555-5555-555555555555",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="NEEDS_INPUT",
    )
    runId = run.run_id

    missing = await client.post(
        f"{_BASE_PATH}/{runId}/resume", headers=OWNER_HEADERS, json={}
    )
    assert missing.status_code == 422, missing.text


async def test_resume_ambiguous_requires_supplementary_input(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="66666666-6666-6666-6666-666666666666",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="AMBIGUOUS",
    )
    runId = run.run_id

    missing = await client.post(
        f"{_BASE_PATH}/{runId}/resume", headers=OWNER_HEADERS, json={}
    )
    assert missing.status_code == 422, missing.text


async def test_resume_budget_exhausted_requires_matching_increment(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="77777777-7777-7777-7777-777777777777",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="BUDGET_EXHAUSTED",
        pauseDetail={"budget": "iteration"},
    )
    runId = run.run_id

    missing = await client.post(
        f"{_BASE_PATH}/{runId}/resume",
        headers=OWNER_HEADERS,
        json={"additionalToolCalls": 1},
    )
    assert missing.status_code == 422, missing.text

    matched = await client.post(
        f"{_BASE_PATH}/{runId}/resume",
        headers=OWNER_HEADERS,
        json={"additionalIterations": 2},
    )
    assert matched.status_code == 202, matched.text
    assert matched.json()["maxIterations"] == 8


async def test_resume_increment_over_system_ceiling_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="88888888-8888-8888-8888-888888888888",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="BUDGET_EXHAUSTED",
        pauseDetail={"budget": "iteration"},
    )
    runId = run.run_id

    resp = await client.post(
        f"{_BASE_PATH}/{runId}/resume",
        headers=OWNER_HEADERS,
        json={"additionalIterations": 9999},
    )
    assert resp.status_code == 422, resp.text


async def test_resume_permission_denied_still_denied_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="99999999-9999-9999-9999-999999999999",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="PERMISSION_DENIED",
    )
    runId = run.run_id

    resp = await client.post(f"{_BASE_PATH}/{runId}/resume", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 409, resp.text
    # 不得泄露策略内部信息
    assert "SUPPLIER" not in resp.text
    assert "policy" not in resp.text.lower()


async def test_resume_permission_denied_succeeds_after_policy_fixed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="PERMISSION_DENIED",
    )
    runId = run.run_id

    policy = await client.post(
        f"/api/v1/agents/{_AGENT_CODE}/policies",
        headers=ADMIN_HEADERS,
        json={"data_object": "SUPPLIER", "permission": "read", "data_layer": "DIM"},
    )
    assert policy.status_code == 201, policy.text

    resp = await client.post(f"{_BASE_PATH}/{runId}/resume", headers=OWNER_HEADERS, json={})

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "QUEUED"
    assert resp.json()["pauseCode"] is None


async def test_successful_resume_clears_pause_fields_and_keeps_model_id(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    run = await _seedRun(
        dbSession,
        runId="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        modelConfigId=modelId,
        status="PAUSED",
        pauseCode="NEEDS_INPUT",
        pauseDetail={"reason": "missing supplier key"},
        question="how many suppliers?",
    )
    runId = run.run_id

    resp = await client.post(
        f"{_BASE_PATH}/{runId}/resume",
        headers=OWNER_HEADERS,
        json={"input": "supplier key is S-001"},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "QUEUED"
    assert body["pauseCode"] is None
    assert body["pauseDetail"] is None
    assert body["cancelRequested"] is False
    assert body["modelConfigId"] == modelId
    assert body["revision"] == 2
    # 补充输入追加在原问题之后，而不是覆盖
    assert "how many suppliers?" in body["question"]
    assert "supplier key is S-001" in body["question"]
