"""验证 seed_agent_tool_bindings 从 _AGENT_DEFAULT_BINDINGS 常量 seed 3 行 + 幂等。"""
import pytest
from sqlalchemy import select
from app.infrastructure.database import getSessionFactory
from app.domain.models import AgentDefinition
from app.domain.enums import (
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
)
from app.dependencies import CurrentUser
from app.domain.schemas import AgentDefinitionCreate
from app.services.agent_registry_service import AgentRegistryService
from scripts.seed_agent_tool_bindings import seed_agent_tool_bindings
from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS

_SEED_ACTOR = CurrentUser(userId="seed-test", roles=("admin",), departments=("procurement",))


async def _ensureAgentsExist() -> None:
    """Brief 测试假设 agent_definition 行已存在；pgApiClient TRUNCATE 后需重新创建。

    仅在 _AGENT_DEFAULT_BINDINGS 的 key 上注册 Agent（不可运行也能注册 —— 测试只关心 tool_name 列），
    用 AgentRegistryService 走 service 层（与 seed_agents.py 一致）。
    """
    factory = getSessionFactory()
    async with factory() as session:
        existing = set(
            (await session.execute(select(AgentDefinition.agent_code))).scalars().all()
        )
        service = AgentRegistryService()
        for code in _AGENT_DEFAULT_BINDINGS:
            if code in existing:
                continue
            await service.createAgent(
                session,
                AgentDefinitionCreate(
                    agent_code=code,
                    agent_name=f"{code} seed_test",
                    description="seed_agent_tool_bindings 测试专用",
                    trigger_type=AgentTriggerType.USER_QUESTION,
                    response_latency=AgentResponseLatency.REALTIME,
                    data_domains=["PROCUREMENT"],
                    data_layers=["FEATURE"],
                    status=AgentStatus.ACTIVE,
                    version="v1.0",
                    policies=[],
                ),
                _SEED_ACTOR,
            )


@pytest.fixture(autouse=True)
async def cleanup(client):
    """清空 tool_name 字段以保证测试独立性（依赖 client 触发 pgApiClient 真实 PG 初始化）。"""
    await _ensureAgentsExist()
    factory = getSessionFactory()
    async with factory() as session:
        await session.execute(
            AgentDefinition.__table__.update().values(tool_name=None)
        )
        await session.commit()
    yield


@pytest.mark.asyncio
async def test_seed_inserts_three_rows_from_AGENT_BINDINGS():
    factory = getSessionFactory()
    async with factory() as session:
        inserted = await seed_agent_tool_bindings(session)
    assert inserted == len(_AGENT_DEFAULT_BINDINGS)  # 3

    async with factory() as session:
        rows = await session.execute(
            select(
                AgentDefinition.agent_code,
                AgentDefinition.tool_name,
                AgentDefinition.tool_name_updated_at,
            ).where(AgentDefinition.tool_name.is_not(None))
        )
        result = {r.agent_code: r for r in rows}
    assert set(result.keys()) == set(_AGENT_DEFAULT_BINDINGS.keys())
    for code, tool_name in _AGENT_DEFAULT_BINDINGS.items():
        assert result[code].tool_name == tool_name
        assert result[code].tool_name_updated_at is not None  # 自动戳


@pytest.mark.asyncio
async def test_seed_is_idempotent():
    factory = getSessionFactory()
    async with factory() as session:
        first = await seed_agent_tool_bindings(session)
        second = await seed_agent_tool_bindings(session)
    assert first == 3
    assert second == 0  # 第二次无新增
