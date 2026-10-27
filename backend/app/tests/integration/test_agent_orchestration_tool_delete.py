"""Task 4 integration tests: orchestration bindings block tool deletion (real PG).

Covers the delete-guard contract:
- A tool bound to an orchestration profile → 409 with the stable
  ``agent_tool_in_use_orchestration`` detail and the sorted agent codes.
- An unbound tool still deletes with the pre-change baseline (204, empty body).
- Legacy ``AgentDefinition.tool_name`` and orchestration references coexist →
  both lists are reported without exposing tool configuration payloads.

Everything is arranged through the public Agent / tool / orchestration APIs and
asserted against real ``agent_tool_config`` rows.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.models import AgentToolConfig
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.tests import _pg_support

ADMIN_HEADERS = {
    "X-User-Id": "orch-delete-admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
OWNER_HEADERS = {
    "X-User-Id": "orch-delete-owner",
    "X-User-Roles": "analyst",
    "X-User-Departments": "procurement",
}

_VALID_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}


@pytest.fixture
async def client(monkeypatch):
    """Open the orchestration feature flag before building the test app."""
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        getSettings.cache_clear()


async def _createTool(client: AsyncClient, name: str) -> None:
    resp = await client.post(
        "/api/v1/agent-tools",
        headers=ADMIN_HEADERS,
        json={
            "name": name,
            "description": f"{name} read-only tool",
            "data_object": "WIKI_PAGE",
            "data_layers": ["DIM"],
            "input_schema": _VALID_SCHEMA,
            "handler_kind": "BUILTIN",
            "handler_ref": "wiki_search",
            "arg_extractor_kind": "wiki_text",
        },
    )
    assert resp.status_code == 201, resp.text


async def _createAgent(
    client: AsyncClient,
    code: str,
    *,
    tool_name: str | None = None,
) -> None:
    resp = await client.post(
        "/api/v1/agents",
        headers=OWNER_HEADERS,
        json={
            "agent_code": code,
            "agent_name": f"{code} name",
            "description": "orchestration delete guard test agent",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM"],
            "status": "active",
            "tool_name": tool_name,
            "policies": [
                {
                    "data_object": "WIKI_PAGE",
                    "permission": "read",
                    "data_layer": "DIM",
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text


async def _createProfile(client: AsyncClient, agentCode: str, toolName: str) -> None:
    resp = await client.post(
        "/api/v1/agent-orchestrations",
        headers=OWNER_HEADERS,
        json={
            "agent_code": agentCode,
            "instructions": "Use the bound read-only tools.",
            "tool_names": [toolName],
            "default_max_iterations": 5,
            "default_max_tool_calls": 5,
            "default_max_cost_usd": 0.5,
            "default_timeout_seconds": 60,
            "max_observation_chars": 2000,
        },
    )
    assert resp.status_code == 201, resp.text


async def _readTool(
    dbSession: AsyncSession, name: str
) -> AgentToolConfig | None:
    return (
        await dbSession.execute(
            select(AgentToolConfig).where(AgentToolConfig.name == name)
        )
    ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_orchestration_binding_blocks_tool_delete(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createTool(client, "orch_delete_probe")
    await _createAgent(client, "ORCH_DELETE_AGENT")
    await _createProfile(client, "ORCH_DELETE_AGENT", "orch_delete_probe")

    response = await client.delete(
        "/api/v1/agent-tools/orch_delete_probe", headers=ADMIN_HEADERS
    )

    assert response.status_code == 409, response.text
    body = response.json()
    detail = body.get("detail") or ""
    assert "orchestration" in detail.lower()
    assert "agent_tool_in_use_orchestration" in detail
    assert "ORCH_DELETE_AGENT" in detail
    remaining = await _readTool(dbSession, "orch_delete_probe")
    assert remaining is not None
    assert remaining.name == "orch_delete_probe"


@pytest.mark.asyncio
async def test_unbound_tool_delete_matches_pre_change_baseline(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createTool(client, "delete_control")

    response = await client.delete(
        "/api/v1/agent-tools/delete_control", headers=ADMIN_HEADERS
    )

    # Baseline before the guard: plain 204 with an empty body.
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert await _readTool(dbSession, "delete_control") is None


@pytest.mark.asyncio
async def test_legacy_and_orchestration_references_report_both_lists(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createTool(client, "orch_delete_probe")
    # The agent tool_name validator reads the DB-backed registry singleton.
    agent_tool_config_registry.invalidate()
    await agent_tool_config_registry.warmUp(dbSession)
    await dbSession.commit()
    await _createAgent(client, "LEGACY_ORCH_AGENT", tool_name="orch_delete_probe")
    await _createProfile(client, "LEGACY_ORCH_AGENT", "orch_delete_probe")

    response = await client.delete(
        "/api/v1/agent-tools/orch_delete_probe", headers=ADMIN_HEADERS
    )

    assert response.status_code == 409, response.text
    detail = response.json().get("detail") or ""
    assert "agent_tool_in_use_orchestration" in detail
    assert "referencingAgents" in detail
    assert "orchestrationAgents" in detail
    assert "LEGACY_ORCH_AGENT" in detail
    assert "input_schema" not in detail
    assert await _readTool(dbSession, "orch_delete_probe") is not None
