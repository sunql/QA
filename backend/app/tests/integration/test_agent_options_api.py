"""Agent Options API 集成测试（真实 PostgreSQL + 完整 API 链路）。

验证 HTTP 契约：
- GET /api/v1/agents/options 返回词表常量
- 路由顺序保护：/options 不被 /{agent_code} 路径吞掉

测试在真实 PG 5433 上运行（qa_metadata_test 数据库）。
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_options_returns_vocabulary(client) -> None:
    """GET /agents/options 返回词表常量；不被 /{agent_code} 路径吞掉。"""
    res = await client.get("/api/v1/agents/options")
    assert res.status_code == 200
    body = res.json()
    assert body["domains"] == ["PROCUREMENT", "QUALITY", "LOGISTICS"]
    assert body["layers"] == ["DIM", "DWD", "FEATURE"]


@pytest.mark.asyncio
async def test_get_options_not_shadowed_by_agent_code_route(client) -> None:
    """路由顺序保护：/options 必须匹配专属路由而非被 /{agent_code} 视为 code='options'（404）。"""
    res = await client.get("/api/v1/agents/options")
    # 如果被吞掉，会走 getAgent(agent_code="options") → 404
    assert res.status_code != 404


@pytest.mark.asyncio
async def test_options_includes_tools_with_all_fields(client) -> None:
    """GET /agents/options 返回 tools 列表：name/description/dataObject/dataLayers 完整。"""
    res = await client.get("/api/v1/agents/options")
    assert res.status_code == 200
    body = res.json()
    assert "tools" in body
    assert isinstance(body["tools"], list)
    assert len(body["tools"]) >= 3  # supplier_360 / supplier_risk / graph_traverse
    by_name = {t["name"]: t for t in body["tools"]}
    assert "supplier_360" in by_name
    assert by_name["supplier_360"]["dataObject"] == "SUPPLIER"
    assert set(by_name["supplier_360"]["dataLayers"]) == {"DIM", "FEATURE"}
    assert by_name["supplier_360"]["description"]  # 非空


@pytest.mark.asyncio
async def test_options_tools_sorted_by_name(client) -> None:
    """tools 列表按 name 升序：registry.all() 返回已排序 → 端点直接透传。"""
    res = await client.get("/api/v1/agents/options")
    names = [t["name"] for t in res.json()["tools"]]
    assert names == sorted(names)
