"""agent-runs API 集成测试的共享件（常量、client 夹具、造数助手）。

模块名以 `_` 开头，pytest 不会把它当测试模块收集（python_files = test_*.py）。
各 subject 模块（create / list / read / resume / cancel / audit）从这里 import
共享件，避免同一份造数逻辑在多个文件里各自漂移。
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
