"""验证 POST/PUT /agents 的 tool_name 写入校验（Phase 7 feat-agent-tool-binding Task 4）。

跨字段一致性：tool_name 必须在 agent_tool_registry 中，且 agent.data_layers
必须完全覆盖 tool.data_layers。非法值 Pydantic 422 拒绝。

注：本项目强制真实 PG（port 5433）；测试通过 integration/conftest.py 的 client fixture
获得完整 API 链路客户端（HTTP → service → 真实 PG），不使用 sqlite 内存库。
"""
import pytest
from httpx import AsyncClient

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
async def test_create_with_valid_tool_and_full_layers_succeeds(client: AsyncClient):
    """tool_name 合法 + data_layers 完全覆盖 → 201。"""
    r = await client.post(
        "/api/v1/agents",
        headers=ADMIN_HEADERS,
        json={
            "agentCode": "BIND_TEST_OK",
            "agentName": "bind ok",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["DIM", "FEATURE"],  # 完全覆盖 supplier_360 的 (DIM, FEATURE)
            "toolName": "supplier_360",
        },
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_create_with_unknown_tool_rejected_422(client: AsyncClient):
    """tool_name 未在 registry 中 → 422 + 错误消息含该名字。"""
    r = await client.post(
        "/api/v1/agents",
        headers=ADMIN_HEADERS,
        json={
            "agentCode": "BIND_TEST_BAD_TOOL",
            "agentName": "bad tool",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["DIM"],
            "toolName": "fake_tool_not_registered",
        },
    )
    assert r.status_code == 422
    assert "fake_tool_not_registered" in r.text


@pytest.mark.asyncio
async def test_create_with_tool_but_missing_layer_rejected_422(client: AsyncClient):
    """tool 合法但 agent.data_layers 未覆盖 tool.data_layers → 422 + 错误消息含缺失层。"""
    r = await client.post(
        "/api/v1/agents",
        headers=ADMIN_HEADERS,
        json={
            "agentCode": "BIND_TEST_MISS_LAYER",
            "agentName": "miss layer",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["DIM"],  # 缺 FEATURE
            "toolName": "supplier_360",
        },
    )
    assert r.status_code == 422
    assert "FEATURE" in r.text


@pytest.mark.asyncio
async def test_create_without_tool_succeeds(client: AsyncClient):
    """toolName 字段未传 = 未绑定 = 元数据 Agent（合法）。"""
    r = await client.post(
        "/api/v1/agents",
        headers=ADMIN_HEADERS,
        json={
            "agentCode": "BIND_TEST_NO_TOOL",
            "agentName": "no tool",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["DIM"],
        },
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_update_tool_to_null_clears_binding(client: AsyncClient):
    """PUT toolName: null 应清空 binding（spec §5.4 行为）。"""
    # 1. 先创建一个有 binding 的 Agent
    r = await client.post(
        "/api/v1/agents",
        headers=ADMIN_HEADERS,
        json={
            "agentCode": "BIND_TEST_CLEAR",
            "agentName": "clear",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["DIM", "FEATURE"],
            "toolName": "supplier_360",
        },
    )
    assert r.status_code == 201, r.text

    # 2. 改 toolName 为 null（PUT 路由用 agent_code 而非 id）
    r2 = await client.put(
        "/api/v1/agents/BIND_TEST_CLEAR",
        headers=ADMIN_HEADERS,
        json={
            "agentName": "clear",
            "dataDomains": ["PROCUREMENT"],
            "dataLayers": ["DIM", "FEATURE"],
            "toolName": None,
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["toolName"] is None