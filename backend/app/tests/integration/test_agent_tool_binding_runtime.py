"""验证 runtime 路径：cache 命中 / dict fallback / 漂移防御。"""
import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.dependencies import CurrentUser
from app.domain.enums import AgentResponseLatency, AgentStatus, AgentTriggerType
from app.domain.models import AgentDefinition
from app.domain.schemas import AgentAccessPolicyCreate, AgentDefinitionCreate
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_registry_service import AgentRegistryService

_AUTH = CurrentUser(userId="admin", roles=("admin",))


async def _seedAgentWithTool(session, code: str, tool_name: str | None) -> None:
    """注册 ACTIVE Agent（tool_name 显式指定）。"""
    svc = AgentRegistryService()
    policies = [
        AgentAccessPolicyCreate(
            data_object="SUPPLIER",
            permission="read",
            data_layer="DIM",
            notes="集成测试",
        ),
        AgentAccessPolicyCreate(
            data_object="SUPPLIER",
            permission="read",
            data_layer="FEATURE",
            notes="集成测试",
        ),
    ]
    dto = AgentDefinitionCreate(
        agent_code=code,
        agent_name=f"{code} 集成测试",
        description="Task 6 集成测试",
        trigger_type=AgentTriggerType.USER_QUESTION,
        response_latency=AgentResponseLatency.REALTIME,
        data_domains=["PROCUREMENT"],
        data_layers=["DIM", "FEATURE"],
        status=AgentStatus.ACTIVE,
        version="v1.0",
        policies=policies,
    )
    await svc.createAgent(session, dto, _AUTH)
    if tool_name is not None:
        await session.execute(
            update(AgentDefinition)
            .where(AgentDefinition.agent_code == code)
            .values(tool_name=tool_name)
        )
        await session.commit()


@pytest.fixture(autouse=True)
async def reset_cache_and_seed_state(dbSession):
    """每个测试前重置 cache + seed agent with tool_name。"""
    agent_binding_cache.invalidate()
    # seed：agent 存在 + tool_name="supplier_360"，warmUp 后 cache 命中
    await _seedAgentWithTool(dbSession, "SUPPLIER_360_AGENT", "supplier_360")
    await agent_binding_cache.warmUp(dbSession)
    yield
    agent_binding_cache.invalidate()


@pytest.mark.asyncio
async def test_run_path_uses_db_binding_via_cache(client: AsyncClient):
    """正常路径：DB 有 binding → cache 命中 → 200。"""
    r = await client.post(
        "/api/v1/agents/SUPPLIER_360_AGENT/run",
        headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
        json={"input": "10105"},
    )
    assert r.status_code in (200, 422)  # 422 也 OK（input 解析失败但走完了 run 路径）
    # 关键：不是 404 / 409 → binding 路径打通


@pytest.mark.asyncio
async def test_run_db_binding_null_cache_miss_raises_409(
    client: AsyncClient, dbSession
):
    """DB binding 清空（tool_name=None）+ cache miss → 409 MSG_AGENT_NOT_RUNNABLE_NO_TOOL。

    验证 feat-agent-tool-binding 核心变更：dict fallback 已删除，
    运行时仅查 cache（来自 DB 的 tool_name），cache 无此 agent 即 409。
    """
    # 清空 tool_name（模拟 DB 未绑定场景）
    await dbSession.execute(
        update(AgentDefinition)
        .where(AgentDefinition.agent_code == "SUPPLIER_360_AGENT")
        .values(tool_name=None)
    )
    await dbSession.commit()
    # 重载 cache 反映新 DB 状态（tool_name=None → cache 无此 agent）
    await agent_binding_cache.warmUp(dbSession)

    r = await client.post(
        "/api/v1/agents/SUPPLIER_360_AGENT/run",
        headers={"X-User-Id": "admin", "X-User-Roles": "admin"},
        json={"input": "10105"},
    )
    # 无 dict fallback，cache 无绑定 → 409（MSG_AGENT_NOT_RUNNABLE_NO_TOOL）
    assert r.status_code == 409
    assert "不可运行" in r.text
