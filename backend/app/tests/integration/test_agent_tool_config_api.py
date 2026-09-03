"""AgentToolConfig REST API 集成测试（feat-agent-tool-config-db, 真实 PG + 完整 API 链路）。

覆盖：
- GET /api/v1/agent-tools → 列表（enabledOnly 过滤）
- GET /api/v1/agent-tools/{name} → 详情（404 路径）
- POST /api/v1/agent-tools → 创建（admin；非 admin 403；duplicate 409；handler combo 422）
- PUT /api/v1/agent-tools/{name} → 更新（optimistic lock；version mismatch 409）
- DELETE /api/v1/agent-tools/{name} → 204；被 Agent 引用 → 409
- POST /api/v1/agent-tools/{name}/toggle → 翻转 enabled
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.models import AgentDefinition, AgentToolConfig
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.services.agent_tool_config_service import AgentToolConfigService

AUTH_ADMIN = {
    "X-User-Id": "admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
AUTH_USER = {
    "X-User-Id": "u1",
    "X-User-Roles": "analyst",
    "X-User-Departments": "procurement",
}
_ADMIN = CurrentUser(userId="admin", roles=("admin",), departments=("IT",))


_TOOL_SEEDS = [
    {
        "name": "supplier_360",
        "description": "查询单供应商 360° 视图",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_360",
        "arg_extractor_kind": "supplier_key",
        "enabled": True,
    },
    {
        "name": "supplier_risk",
        "description": "评估单供应商风险等级",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_risk",
        "arg_extractor_kind": "supplier_risk_key",
        "enabled": True,
    },
    {
        "name": "graph_traverse",
        "description": "供应链链路推理",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "DWD"],
        "handler_kind": "BUILTIN",
        "handler_ref": "graph_traverse",
        "arg_extractor_kind": "supplier_graph_key",
        "enabled": True,
    },
]


@pytest.fixture(autouse=True)
async def _warmToolRegistry(dbSession):
    """Seed 3 tool configs + warmUp registry + binding cache（lifespan 路径）。"""
    service = AgentToolConfigService()
    for seed in _TOOL_SEEDS:
        await service.upsertSeed(dbSession, seed["name"], seed)
    await dbSession.commit()
    agent_tool_config_registry.invalidate()
    await agent_tool_config_registry.warmUp(dbSession)
    agent_binding_cache.invalidate()
    await agent_binding_cache.warmUp(dbSession)
    yield
    agent_tool_config_registry.invalidate()
    agent_binding_cache.invalidate()


class TestListTools:
    async def test_list_returns_all_tools(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/agent-tools", headers=AUTH_USER)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = {row["name"] for row in body}
        assert {"supplier_360", "supplier_risk", "graph_traverse"} <= names

    async def test_list_enabledOnly_filters_disabled(self, client: AsyncClient, dbSession: AsyncSession) -> None:
        # 把 supplier_risk 改为 disabled
        row = (
            await dbSession.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == "supplier_risk")
            )
        ).scalar_one()
        row.enabled = False
        await dbSession.commit()
        agent_tool_config_registry.invalidate()

        resp = await client.get(
            "/api/v1/agent-tools?enabledOnly=true", headers=AUTH_USER
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert all(row["enabled"] for row in body)
        assert "supplier_risk" not in {r["name"] for r in body}


class TestGetTool:
    async def test_get_returns_tool(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/agent-tools/supplier_360", headers=AUTH_USER)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "supplier_360"
        assert body["dataObject"] == "SUPPLIER"
        assert body["handlerKind"] == "BUILTIN"

    async def test_get_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/agent-tools/nonexistent", headers=AUTH_USER)
        assert resp.status_code == 404, resp.text


class TestCreateTool:
    async def test_create_201(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_ADMIN,
            json={
                "name": "test_create",
                "data_object": "supplier",
                "handler_kind": "BUILTIN",
                "handler_ref": "supplier_360",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "test_create"
        assert body["dataObject"] == "SUPPLIER"  # normalized

    async def test_create_403_for_non_admin(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_USER,
            json={
                "name": "test_403",
                "data_object": "SUPPLIER",
                "handler_kind": "BUILTIN",
                "handler_ref": "supplier_360",
            },
        )
        assert resp.status_code == 403, resp.text

    async def test_create_409_on_duplicate(self, client: AsyncClient) -> None:
        payload = {
            "name": "test_dup",
            "data_object": "SUPPLIER",
            "handler_kind": "BUILTIN",
            "handler_ref": "supplier_360",
        }
        r1 = await client.post("/api/v1/agent-tools", headers=AUTH_ADMIN, json=payload)
        assert r1.status_code == 201, r1.text
        r2 = await client.post("/api/v1/agent-tools", headers=AUTH_ADMIN, json=payload)
        assert r2.status_code == 409, r2.text

    async def test_create_422_on_bad_handler_combo(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_ADMIN,
            json={
                "name": "test_422",
                "data_object": "SUPPLIER",
                "handler_kind": "BUILTIN",
                "handler_ref": "nl2sql_default",  # NL2SQL ref under BUILTIN kind
            },
        )
        assert resp.status_code == 422, resp.text


class TestUpdateTool:
    async def test_update_with_version_match(self, client: AsyncClient) -> None:
        create_r = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_ADMIN,
            json={
                "name": "test_upd",
                "data_object": "SUPPLIER",
                "handler_kind": "BUILTIN",
                "handler_ref": "supplier_360",
            },
        )
        assert create_r.status_code == 201, create_r.text
        v = create_r.json()["version"]
        r = await client.put(
            "/api/v1/agent-tools/test_upd",
            headers=AUTH_ADMIN,
            json={"version": v, "description": "updated"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["description"] == "updated"
        assert body["version"] == v + 1

    async def test_update_409_on_version_mismatch(self, client: AsyncClient) -> None:
        create_r = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_ADMIN,
            json={
                "name": "test_upd_mismatch",
                "data_object": "SUPPLIER",
                "handler_kind": "BUILTIN",
                "handler_ref": "supplier_360",
            },
        )
        assert create_r.status_code == 201, create_r.text
        r = await client.put(
            "/api/v1/agent-tools/test_upd_mismatch",
            headers=AUTH_ADMIN,
            json={"version": 999, "description": "x"},
        )
        assert r.status_code == 409, r.text

    async def test_update_403_for_non_admin(self, client: AsyncClient) -> None:
        create_r = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_ADMIN,
            json={
                "name": "test_upd_403",
                "data_object": "SUPPLIER",
                "handler_kind": "BUILTIN",
                "handler_ref": "supplier_360",
            },
        )
        assert create_r.status_code == 201, create_r.text
        v = create_r.json()["version"]
        r = await client.put(
            "/api/v1/agent-tools/test_upd_403",
            headers=AUTH_USER,
            json={"version": v, "description": "x"},
        )
        assert r.status_code == 403, r.text


class TestDeleteTool:
    async def test_delete_204(self, client: AsyncClient) -> None:
        create_r = await client.post(
            "/api/v1/agent-tools",
            headers=AUTH_ADMIN,
            json={
                "name": "test_del",
                "data_object": "SUPPLIER",
                "handler_kind": "BUILTIN",
                "handler_ref": "supplier_360",
            },
        )
        assert create_r.status_code == 201, create_r.text
        r = await client.delete("/api/v1/agent-tools/test_del", headers=AUTH_ADMIN)
        assert r.status_code == 204, r.text
        # 再读 → 404
        g = await client.get("/api/v1/agent-tools/test_del", headers=AUTH_ADMIN)
        assert g.status_code == 404

    async def test_delete_403_for_non_admin(self, client: AsyncClient) -> None:
        r = await client.delete(
            "/api/v1/agent-tools/supplier_360", headers=AUTH_USER
        )
        assert r.status_code == 403, r.text

    async def test_delete_409_when_referenced_by_agent(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        # seed 一条 Agent 引用 supplier_360
        dbSession.add(
            AgentDefinition(
                agent_code="REFERENCING_AGENT",
                agent_name="referencing",
                description="x",
                trigger_type="user_question",
                response_latency="realtime",
                data_domains=["PROCUREMENT"],
                data_layers=["FEATURE"],
                status="active",
                version="v1.0",
                tool_name="supplier_360",
            )
        )
        await dbSession.commit()

        r = await client.delete(
            "/api/v1/agent-tools/supplier_360", headers=AUTH_ADMIN
        )
        assert r.status_code == 409, r.text
        # 详情里应含 referencingAgents 字段
        body = r.json()
        body_text = str(body)
        assert "referencingAgents" in body_text or "REFERENCING_AGENT" in body_text


class TestToggleTool:
    async def test_toggle_200(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/agent-tools/supplier_360/toggle",
            headers=AUTH_ADMIN,
            json={"enabled": False},
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is False
        # 再 toggle 回 True
        r = await client.post(
            "/api/v1/agent-tools/supplier_360/toggle",
            headers=AUTH_ADMIN,
            json={"enabled": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled"] is True

    async def test_toggle_403_for_non_admin(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/agent-tools/supplier_360/toggle",
            headers=AUTH_USER,
            json={"enabled": False},
        )
        assert r.status_code == 403, r.text

    async def test_toggle_404_for_unknown(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/agent-tools/nonexistent/toggle",
            headers=AUTH_ADMIN,
            json={"enabled": False},
        )
        assert r.status_code == 404, r.text