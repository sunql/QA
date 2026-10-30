"""Agent orchestration 配置 API 集成测试（真实 PostgreSQL + 完整 API 链路）。

覆盖 Task 3 的接口契约：
- feature flag 关闭时整组路由不存在（404）
- profile 创建（201 / enabled=false / revision=1 / 绑定顺序）/ 查询 / 列表
- 乐观锁（revision 不匹配 → 409）与 toggle 显式启停
- owner ACL 写权限（无关非 admin → 403）与列表按 owner 过滤
- 绑定工具校验（禁用 / 非白名单 handler / 非法 schema / 超系统预算 → 422）
- GET /agent-orchestrations/tool-options 只返回启用、有效、只读白名单工具

所有工具与 Agent 均通过既有公开 API 创建，断言落在真实行上。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.config import getSettings
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

_BASE_PATH = "/api/v1/agent-orchestrations"


@pytest.fixture
async def client(monkeypatch):
    """本地覆盖 client：先打开 feature flag，再构建测试 app。

    dbSession（integration conftest）依赖本 fixture，因此仍使用同一引擎。
    """
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        getSettings.cache_clear()


async def _createAgent(
    client: AsyncClient,
    code: str = "ORCH_TEST_AGENT",
    headers: dict[str, str] | None = None,
) -> None:
    resp = await client.post(
        "/api/v1/agents",
        headers=headers or OWNER_HEADERS,
        json={
            "agent_code": code,
            "agent_name": f"{code} name",
            "description": "orchestration config test agent",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM"],
            "status": "active",
        },
    )
    assert resp.status_code == 201, resp.text


async def _createTool(
    client: AsyncClient,
    name: str,
    *,
    handlerRef: str = "wiki_search",
    handlerKind: str = "BUILTIN",
    inputSchema: dict | None = None,
) -> None:
    payload: dict = {
        "name": name,
        "description": f"{name} description",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM"],
        "handler_kind": handlerKind,
        "handler_ref": handlerRef,
    }
    if inputSchema is not None:
        payload["input_schema"] = inputSchema
    resp = await client.post(
        "/api/v1/agent-tools", headers=ADMIN_HEADERS, json=payload
    )
    assert resp.status_code == 201, resp.text


async def _disableTool(client: AsyncClient, name: str) -> None:
    resp = await client.post(
        f"/api/v1/agent-tools/{name}/toggle",
        headers=ADMIN_HEADERS,
        json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text


def _profilePayload(
    agentCode: str,
    toolNames: list[str],
    **overrides,
) -> dict:
    payload = {
        "agent_code": agentCode,
        "instructions": "Answer using the bound read-only tools.",
        "tool_names": toolNames,
        "default_max_iterations": 10,
        "default_max_tool_calls": 10,
        "default_max_cost_usd": 1.0,
        "default_timeout_seconds": 120,
        "max_observation_chars": 5000,
    }
    payload.update(overrides)
    return payload


async def test_disabled_feature_returns_404(monkeypatch):
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            listResponse = await ac.get(_BASE_PATH, headers=OWNER_HEADERS)
            toolOptionsResponse = await ac.get(
                f"{_BASE_PATH}/tool-options", headers=OWNER_HEADERS
            )
        assert listResponse.status_code == 404
        assert toolOptionsResponse.status_code == 404
    finally:
        getSettings.cache_clear()


async def test_create_returns_201_disabled_revision_one_and_ordered_bindings(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client, "orch_tool_a", inputSchema=_VALID_SCHEMA)
    await _createTool(client, "orch_tool_b", inputSchema=_VALID_SCHEMA)

    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload(
            "ORCH_TEST_AGENT", ["orch_tool_b", "orch_tool_a", "orch_tool_b"]
        ),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["agentCode"] == "ORCH_TEST_AGENT"
    assert body["enabled"] is False
    assert body["revision"] == 1
    assert [b["toolName"] for b in body["bindings"]] == [
        "orch_tool_b",
        "orch_tool_a",
    ]
    assert [b["sortOrder"] for b in body["bindings"]] == [0, 1]


async def test_duplicate_create_conflicts(client: AsyncClient) -> None:
    await _createAgent(client)
    payload = _profilePayload("ORCH_TEST_AGENT", [])

    first = await client.post(_BASE_PATH, headers=OWNER_HEADERS, json=payload)
    second = await client.post(_BASE_PATH, headers=OWNER_HEADERS, json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text


async def test_get_and_list_return_profile_without_changing_agent_options(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client, "orch_tool_a", inputSchema=_VALID_SCHEMA)
    optionsBefore = await client.get("/api/v1/agents/options", headers=OWNER_HEADERS)
    assert optionsBefore.status_code == 200

    created = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", ["orch_tool_a"]),
    )
    assert created.status_code == 201, created.text

    detail = await client.get(
        f"{_BASE_PATH}/ORCH_TEST_AGENT", headers=OWNER_HEADERS
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["agentCode"] == "ORCH_TEST_AGENT"

    listing = await client.get(_BASE_PATH, headers=OWNER_HEADERS)
    assert listing.status_code == 200, listing.text
    codes = [row["agentCode"] for row in listing.json()]
    assert codes == ["ORCH_TEST_AGENT"]

    optionsAfter = await client.get("/api/v1/agents/options", headers=OWNER_HEADERS)
    assert optionsAfter.status_code == 200
    assert optionsAfter.json() == optionsBefore.json()


async def test_get_missing_profile_returns_404(client: AsyncClient) -> None:
    await _createAgent(client)
    resp = await client.get(f"{_BASE_PATH}/ORCH_TEST_AGENT", headers=OWNER_HEADERS)
    assert resp.status_code == 404, resp.text


async def test_update_with_stale_revision_returns_409(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client, "orch_tool_a", inputSchema=_VALID_SCHEMA)
    created = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", ["orch_tool_a"]),
    )
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]

    resp = await client.put(
        f"{_BASE_PATH}/ORCH_TEST_AGENT",
        headers=OWNER_HEADERS,
        json={
            "instructions": "stale update",
            "tool_names": ["orch_tool_a"],
            "default_max_iterations": 5,
            "default_max_tool_calls": 5,
            "default_max_cost_usd": 1.0,
            "default_timeout_seconds": 60,
            "max_observation_chars": 5000,
            "revision": revision + 99,
        },
    )
    assert resp.status_code == 409, resp.text


async def test_update_replaces_bindings_and_increments_revision(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client, "orch_tool_a", inputSchema=_VALID_SCHEMA)
    await _createTool(client, "orch_tool_b", inputSchema=_VALID_SCHEMA)
    created = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", ["orch_tool_a"]),
    )
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]

    resp = await client.put(
        f"{_BASE_PATH}/ORCH_TEST_AGENT",
        headers=OWNER_HEADERS,
        json={
            "instructions": "updated instructions",
            "tool_names": ["orch_tool_b"],
            "default_max_iterations": 7,
            "default_max_tool_calls": 8,
            "default_max_cost_usd": 2.0,
            "default_timeout_seconds": 240,
            "max_observation_chars": 6000,
            "revision": revision,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["revision"] == revision + 1
    assert body["instructions"] == "updated instructions"
    assert body["defaultMaxIterations"] == 7
    assert [b["toolName"] for b in body["bindings"]] == ["orch_tool_b"]


async def test_toggle_explicitly_enables_and_disables(client: AsyncClient) -> None:
    await _createAgent(client)
    created = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", []),
    )
    assert created.status_code == 201, created.text
    revision = created.json()["revision"]

    enabled = await client.post(
        f"{_BASE_PATH}/ORCH_TEST_AGENT/toggle",
        headers=OWNER_HEADERS,
        json={"enabled": True, "revision": revision},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True
    assert enabled.json()["revision"] == revision + 1

    disabled = await client.post(
        f"{_BASE_PATH}/ORCH_TEST_AGENT/toggle",
        headers=OWNER_HEADERS,
        json={"enabled": False, "revision": enabled.json()["revision"]},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["revision"] == revision + 2


async def test_writes_emit_audit_outbox_with_before_and_after(
    client: AsyncClient, dbSession
) -> None:
    from sqlalchemy import select

    from app.domain.models import AuditOutbox

    await _createAgent(client)
    created = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", []),
    )
    assert created.status_code == 201, created.text
    toggled = await client.post(
        f"{_BASE_PATH}/ORCH_TEST_AGENT/toggle",
        headers=OWNER_HEADERS,
        json={"enabled": True, "revision": created.json()["revision"]},
    )
    assert toggled.status_code == 200, toggled.text

    rows = (
        (
            await dbSession.execute(
                select(AuditOutbox)
                .where(AuditOutbox.entity_type == "agent_orchestration_profile")
                .order_by(AuditOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.event_type for row in rows] == [
        "agent_orchestration_profile_created",
        "agent_orchestration_profile_updated",
    ]
    assert rows[0].payload["before"] is None
    assert rows[1].payload["before"]["enabled"] is False
    assert rows[1].payload["after"]["enabled"] is True
    assert rows[1].payload["after"]["revision"] == 2


async def test_owner_can_write_and_unrelated_non_admin_gets_403(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    payload = _profilePayload("ORCH_TEST_AGENT", [])

    ownerWrite = await client.post(_BASE_PATH, headers=OWNER_HEADERS, json=payload)
    assert ownerWrite.status_code == 201, ownerWrite.text

    outsiderWrite = await client.post(_BASE_PATH, headers=OUTSIDER_HEADERS, json=payload)
    assert outsiderWrite.status_code == 403, outsiderWrite.text

    outsiderToggle = await client.post(
        f"{_BASE_PATH}/ORCH_TEST_AGENT/toggle",
        headers=OUTSIDER_HEADERS,
        json={"enabled": True, "revision": 1},
    )
    assert outsiderToggle.status_code == 403, outsiderToggle.text

    ownerList = await client.get(_BASE_PATH, headers=OWNER_HEADERS)
    outsiderList = await client.get(_BASE_PATH, headers=OUTSIDER_HEADERS)
    adminList = await client.get(_BASE_PATH, headers=ADMIN_HEADERS)
    assert [row["agentCode"] for row in ownerList.json()] == ["ORCH_TEST_AGENT"]
    assert outsiderList.json() == []
    assert [row["agentCode"] for row in adminList.json()] == ["ORCH_TEST_AGENT"]


async def test_create_rejects_disabled_tool(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client, "orch_tool_disabled", inputSchema=_VALID_SCHEMA)
    await _disableTool(client, "orch_tool_disabled")

    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", ["orch_tool_disabled"]),
    )
    assert resp.status_code == 422, resp.text


async def test_create_rejects_unknown_handler(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(
        client,
        "orch_tool_nl2sql",
        handlerRef="nl2sql_default",
        handlerKind="NL2SQL",
        inputSchema=_VALID_SCHEMA,
    )

    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", ["orch_tool_nl2sql"]),
    )
    assert resp.status_code == 422, resp.text


async def test_create_rejects_invalid_tool_schema(client: AsyncClient) -> None:
    await _createAgent(client)
    await _createTool(client, "orch_tool_bad_schema")  # input_schema 默认 {}

    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload("ORCH_TEST_AGENT", ["orch_tool_bad_schema"]),
    )
    assert resp.status_code == 422, resp.text


async def test_create_rejects_budget_above_system_ceiling(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    settings = getSettings()

    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json=_profilePayload(
            "ORCH_TEST_AGENT",
            [],
            default_max_iterations=settings.agentOrchestrationMaxIterations + 1,
        ),
    )
    assert resp.status_code == 422, resp.text


async def test_tool_options_lists_only_enabled_valid_whitelisted_tools(
    client: AsyncClient,
) -> None:
    await _createTool(client, "orch_alpha", inputSchema=_VALID_SCHEMA)
    await _createTool(client, "orch_zeta", inputSchema=_VALID_SCHEMA)
    await _createTool(client, "orch_beta", inputSchema=_VALID_SCHEMA)
    await _disableTool(client, "orch_beta")
    await _createTool(client, "orch_gamma")  # 无效 schema（{}）
    await _createTool(
        client,
        "orch_delta",
        handlerRef="nl2sql_default",
        handlerKind="NL2SQL",
        inputSchema=_VALID_SCHEMA,
    )

    resp = await client.get(f"{_BASE_PATH}/tool-options", headers=OWNER_HEADERS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [option["toolName"] for option in body] == ["orch_alpha", "orch_zeta"]
    for option in body:
        assert set(option.keys()) == {"toolName", "description", "inputSchema"}
        assert "handlerRef" not in option
        assert option["inputSchema"] == _VALID_SCHEMA
        assert option["description"] == f"{option['toolName']} description"
