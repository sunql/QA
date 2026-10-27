"""Task 7 集成测试：编排运行的 run 生命周期（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- feature flag 关闭时整组路由不存在（404）
- create：必须带 modelConfigId、模型必须存在且 active、快照 instructions/bindings/schema/预算、
  返回 202 QUEUED、计数归零、cost_source=UNKNOWN
- create 失败路径（缺/禁用 profile、Agent 非 ACTIVE、无有效绑定、超系统预算）→ 409/422 且零行
- list：按 agentCode/status 过滤、limit/offset 有界、只返回调用者自己的 run
- 读 ACL：他人的 run 一律 404（get/steps/resume/cancel 同一文案），admin 可读
- steps：stepNo 升序、afterStepNo 生效
- resume：仅 PAUSED；NEEDS_INPUT/AMBIGUOUS 需补充输入；BUDGET_EXHAUSTED 需对应增量；
  PERMISSION_DENIED 复查当前 ACL；成功后清空暂停字段、模型 id 不变
- cancel：QUEUED/PAUSED → CANCELLED；RUNNING → cancelRequested=true；终态 → 409
- 历史 run 在 Agent 被硬删除后仍可读

所有 Agent / 工具 / profile / 模型均通过既有公开 API 创建；PAUSED / RUNNING 等
中间态 run 直接落库造数（真实 PostgreSQL 行）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
)
from app.domain.models import AuditOutbox
from app.tests import _pg_support

ADMIN_HEADERS = {
    "X-User-Id": "orch-admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
OWNER_HEADERS = {
    "X-User-Id": "orch-owner",
    "X-User-Roles": "analyst",
    "X-User-Departments": "procurement",
}
OUTSIDER_HEADERS = {
    "X-User-Id": "orch-outsider",
    "X-User-Roles": "analyst",
    "X-User-Departments": "sales",
}

_VALID_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}

_BASE_PATH = "/api/v1/agent-runs"
_CONFIG_PATH = "/api/v1/agent-orchestrations"
_AGENT_CODE = "ORCH_RUN_AGENT"
_TOOL_NAME = "orch_run_tool"


@pytest.fixture
async def client(monkeypatch):
    """本地覆盖 client：先打开 feature flag，再构建测试 app。"""
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        getSettings.cache_clear()


# ---------------------------------------------------------------------------
# 造数助手（全部走公开 API，除显式标注的直接落库）
# ---------------------------------------------------------------------------


async def _createAgent(
    client: AsyncClient,
    code: str = _AGENT_CODE,
    *,
    status: str = "active",
    policies: list[dict] | None = None,
) -> None:
    payload: dict = {
        "agent_code": code,
        "agent_name": f"{code} name",
        "description": "orchestration run test agent",
        "data_domains": ["PROCUREMENT"],
        "data_layers": ["DIM"],
        "status": status,
    }
    if policies is not None:
        payload["policies"] = policies
    resp = await client.post("/api/v1/agents", headers=OWNER_HEADERS, json=payload)
    assert resp.status_code == 201, resp.text


async def _createTool(
    client: AsyncClient,
    name: str = _TOOL_NAME,
    *,
    inputSchema: dict | None = _VALID_SCHEMA,
    handlerRef: str = "wiki_search",
) -> None:
    payload: dict = {
        "name": name,
        "description": f"{name} description",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM"],
        "handler_kind": "BUILTIN",
        "handler_ref": handlerRef,
    }
    if inputSchema is not None:
        payload["input_schema"] = inputSchema
    resp = await client.post("/api/v1/agent-tools", headers=ADMIN_HEADERS, json=payload)
    assert resp.status_code == 201, resp.text


async def _disableTool(client: AsyncClient, name: str = _TOOL_NAME) -> None:
    resp = await client.post(
        f"/api/v1/agent-tools/{name}/toggle",
        headers=ADMIN_HEADERS,
        json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text


async def _createProfile(
    client: AsyncClient,
    *,
    agentCode: str = _AGENT_CODE,
    toolNames: list[str] | None = None,
    enabled: bool = True,
    **overrides,
) -> None:
    payload: dict = {
        "agent_code": agentCode,
        "instructions": "Answer using the bound read-only tools.",
        "tool_names": [_TOOL_NAME] if toolNames is None else toolNames,
        "default_max_iterations": 6,
        "default_max_tool_calls": 8,
        "default_max_cost_usd": 1.0,
        "default_timeout_seconds": 120,
        "max_observation_chars": 5000,
    }
    payload.update(overrides)
    created = await client.post(_CONFIG_PATH, headers=OWNER_HEADERS, json=payload)
    assert created.status_code == 201, created.text
    if enabled:
        toggled = await client.post(
            f"{_CONFIG_PATH}/{agentCode}/toggle",
            headers=OWNER_HEADERS,
            json={"enabled": True, "revision": created.json()["revision"]},
        )
        assert toggled.status_code == 200, toggled.text


async def _createModel(
    client: AsyncClient,
    name: str = "orch-run-model",
    *,
    isActive: bool = True,
) -> int:
    resp = await client.post(
        "/api/v1/models",
        headers=ADMIN_HEADERS,
        json={
            "model_name": name,
            "provider": "openai",
            "cost_per_1k_input": 0.001,
            "cost_per_1k_output": 0.002,
            "is_active": isActive,
        },
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _createRun(
    client: AsyncClient,
    *,
    agentCode: str = _AGENT_CODE,
    modelConfigId: int,
    limits: dict | None = None,
    headers: dict[str, str] | None = None,
):
    payload: dict = {
        "agentCode": agentCode,
        "input": "how many suppliers?",
        "modelConfigId": modelConfigId,
    }
    if limits is not None:
        payload["limits"] = limits
    return await client.post(_BASE_PATH, headers=headers or OWNER_HEADERS, json=payload)


async def _seedRun(
    dbSession: AsyncSession,
    *,
    runId: str,
    modelConfigId: int,
    status: str = "PAUSED",
    pauseCode: str | None = None,
    pauseDetail: dict | None = None,
    createdBy: str = "orch-owner",
    agentCode: str = _AGENT_CODE,
    toolNames: list[str] | None = None,
    question: str = "how many suppliers?",
    maxIterations: int = 6,
    maxToolCalls: int = 8,
) -> AgentOrchestrationRun:
    """直接落库造一个指定状态的 run（QUEUED/RUNNING/PAUSED/终态）。"""
    names = [_TOOL_NAME] if toolNames is None else toolNames
    run = AgentOrchestrationRun(
        run_id=runId,
        agent_id=None,
        agent_code=agentCode,
        profile_revision=1,
        instructions_snapshot="Answer using the bound read-only tools.",
        tool_bindings_snapshot=[
            {
                "toolName": name,
                "description": f"{name} description",
                "inputSchema": _VALID_SCHEMA,
                "sortOrder": index,
            }
            for index, name in enumerate(names)
        ],
        max_observation_chars=5000,
        question=question,
        created_by=createdBy,
        model_config_id=modelConfigId,
        status=status,
        max_iterations=maxIterations,
        max_tool_calls=maxToolCalls,
        max_cost_usd=Decimal("1"),
        timeout_seconds=120,
        pause_code=pauseCode,
        pause_detail=pauseDetail,
    )
    dbSession.add(run)
    await dbSession.commit()
    await dbSession.refresh(run)
    return run


async def _seedStep(
    dbSession: AsyncSession,
    run: AgentOrchestrationRun,
    stepNo: int,
    *,
    kind: str = "TOOL",
    status: str = "SUCCEEDED",
) -> AgentOrchestrationStep:
    step = AgentOrchestrationStep(
        run_id=run.id,
        step_no=stepNo,
        iteration_no=0,
        kind=kind,
        status=status,
        tool_name=_TOOL_NAME if kind == "TOOL" else None,
        started_at=datetime.now(UTC),
    )
    dbSession.add(step)
    await dbSession.commit()
    await dbSession.refresh(step)
    return step


async def _countRuns(dbSession: AsyncSession) -> int:
    return int(
        (
            await dbSession.execute(select(func.count()).select_from(AgentOrchestrationRun))
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# feature flag
# ---------------------------------------------------------------------------


async def test_disabled_feature_returns_404(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            listing = await ac.get(_BASE_PATH, headers=OWNER_HEADERS)
            created = await ac.post(
                _BASE_PATH,
                headers=OWNER_HEADERS,
                json={
                    "agentCode": _AGENT_CODE,
                    "input": "hi",
                    "modelConfigId": 1,
                },
            )
    finally:
        getSettings.cache_clear()
    assert listing.status_code == 404
    assert created.status_code == 404
    # 必须是「路由不存在」的 404（FastAPI 默认体），而不是领域异常的 404
    assert listing.json() == {"detail": "Not Found"}
    assert created.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_requires_model_config_id(client: AsyncClient) -> None:
    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json={"agentCode": _AGENT_CODE, "input": "hi"},
    )
    assert resp.status_code == 422, resp.text


async def test_create_returns_202_queued_with_snapshots(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "QUEUED"
    runId = body["runId"]
    assert isinstance(runId, str) and len(runId) == 36

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=OWNER_HEADERS)
    assert detail.status_code == 200, detail.text
    run = detail.json()
    assert run["runId"] == runId
    assert run["agentCode"] == _AGENT_CODE
    assert run["createdBy"] == "orch-owner"
    assert run["modelConfigId"] == modelId
    assert run["status"] == "QUEUED"
    assert run["instructionsSnapshot"] == "Answer using the bound read-only tools."
    assert run["toolBindingsSnapshot"] == [
        {
            "toolName": _TOOL_NAME,
            "description": f"{_TOOL_NAME} description",
            "inputSchema": _VALID_SCHEMA,
            "sortOrder": 0,
        }
    ]
    assert run["maxIterations"] == 6
    assert run["maxToolCalls"] == 8
    assert run["maxCostUsd"] == 1.0
    assert run["timeoutSeconds"] == 120
    assert run["iterationCount"] == 0
    assert run["toolCallCount"] == 0
    assert run["totalTokens"] == 0
    assert run["cost"] == 0.0
    assert run["costSource"] == "UNKNOWN"
    assert run["costComplete"] is False
    assert run["pauseCode"] is None
    assert run["cancelRequested"] is False
    assert run["revision"] == 1
    assert run["startedAt"] is None
    assert run["finishedAt"] is None

    # 快照内不得出现工具配置内部字段
    snapshotText = str(run["toolBindingsSnapshot"])
    assert "handlerRef" not in snapshotText
    assert "handler_ref" not in snapshotText
    assert "wiki_search" not in snapshotText
    assert await _countRuns(dbSession) == 1


async def test_create_merges_provided_limits_over_profile_defaults(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(
        client,
        modelConfigId=modelId,
        limits={"maxIterations": 3, "timeoutSeconds": 45},
    )
    assert resp.status_code == 202, resp.text

    detail = await client.get(f"{_BASE_PATH}/{resp.json()['runId']}", headers=OWNER_HEADERS)
    run = detail.json()
    assert run["maxIterations"] == 3
    assert run["timeoutSeconds"] == 45
    # 未覆盖的字段回落 profile 默认值
    assert run["maxToolCalls"] == 8
    assert run["maxCostUsd"] == 1.0


async def test_create_with_unknown_model_returns_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)

    resp = await _createRun(client, modelConfigId=987654)

    assert resp.status_code == 404, resp.text
    # 领域 404（而非路由不存在）
    assert "error" in resp.json()
    assert resp.json()["error"] != "Not Found"
    assert await _countRuns(dbSession) == 0


async def test_create_with_inactive_model_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client, "orch-run-inactive-model", isActive=False)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_without_profile_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_with_disabled_profile_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client, enabled=False)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_with_non_active_agent_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client, status="draft")
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_without_valid_binding_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    # 工具被禁用后，绑定不再「当前有效」
    await _disableTool(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_with_limits_over_system_ceiling_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId, limits={"maxIterations": 9999})

    assert resp.status_code == 422, resp.text
    assert await _countRuns(dbSession) == 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_only_callers_runs_and_filters(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    created = await _createRun(client, modelConfigId=modelId)
    assert created.status_code == 202, created.text
    mine = created.json()["runId"]

    outsider = await _createRun(
        client, modelConfigId=modelId, headers=OUTSIDER_HEADERS
    )
    assert outsider.status_code == 202, outsider.text
    theirs = outsider.json()["runId"]

    listing = await client.get(_BASE_PATH, headers=OWNER_HEADERS)
    assert listing.status_code == 200, listing.text
    ids = [row["runId"] for row in listing.json()]
    assert ids == [mine]
    assert theirs not in ids

    byAgent = await client.get(
        f"{_BASE_PATH}?agentCode={_AGENT_CODE}", headers=OWNER_HEADERS
    )
    assert byAgent.status_code == 200, byAgent.text
    assert [row["runId"] for row in byAgent.json()] == [mine]

    otherAgent = await client.get(
        f"{_BASE_PATH}?agentCode=OTHER_AGENT", headers=OWNER_HEADERS
    )
    assert otherAgent.status_code == 200, otherAgent.text
    assert otherAgent.json() == []

    byStatus = await client.get(f"{_BASE_PATH}?status=PAUSED", headers=OWNER_HEADERS)
    assert byStatus.status_code == 200, byStatus.text
    assert byStatus.json() == []

    byQueued = await client.get(f"{_BASE_PATH}?status=QUEUED", headers=OWNER_HEADERS)
    assert byQueued.status_code == 200, byQueued.text
    assert [row["runId"] for row in byQueued.json()] == [mine]


async def test_list_honors_bounded_limit_and_offset(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    createdIds = []
    for _ in range(3):
        resp = await _createRun(client, modelConfigId=modelId)
        assert resp.status_code == 202, resp.text
        createdIds.append(resp.json()["runId"])

    limited = await client.get(f"{_BASE_PATH}?limit=2", headers=OWNER_HEADERS)
    assert limited.status_code == 200, limited.text
    assert len(limited.json()) == 2

    offset = await client.get(f"{_BASE_PATH}?limit=2&offset=1", headers=OWNER_HEADERS)
    assert offset.status_code == 200, offset.text
    assert len(offset.json()) == 2
    assert set(createdIds) >= {row["runId"] for row in offset.json()}

    # limit 被压到上限 200：请求 5000 也只返回全部 3 条，不报错
    capped = await client.get(f"{_BASE_PATH}?limit=5000", headers=OWNER_HEADERS)
    assert capped.status_code == 200, capped.text
    assert len(capped.json()) == 3


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


# ---------------------------------------------------------------------------
# 历史数据 + 审计
# ---------------------------------------------------------------------------


async def test_run_survives_agent_deletion(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    deleted = await client.delete(
        f"/api/v1/agents/{_AGENT_CODE}?hard_delete=true", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 200, deleted.text

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=OWNER_HEADERS)
    assert detail.status_code == 200, detail.text
    assert detail.json()["runId"] == runId

    steps = await client.get(f"{_BASE_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    assert steps.status_code == 200, steps.text

    listing = await client.get(_BASE_PATH, headers=OWNER_HEADERS)
    assert listing.status_code == 200, listing.text
    assert [row["runId"] for row in listing.json()] == [runId]


async def test_create_and_cancel_emit_audit_outbox_without_secrets(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    created = await _createRun(client, modelConfigId=modelId)
    runId = created.json()["runId"]

    rows = (
        (
            await dbSession.execute(
                select(AuditOutbox)
                .where(AuditOutbox.entity_type == "agent_orchestration_run")
                .order_by(AuditOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_type == "agent_orchestration_run_created"
    payload = rows[0].payload or {}
    assert payload["after"]["runId"] == runId
    payloadText = str(payload)
    assert "handler_ref" not in payloadText
    assert "handlerRef" not in payloadText
    assert "wiki_search" not in payloadText
    assert "api_key" not in payloadText
    assert "apiKey" not in payloadText
