"""seed_agents._policiesFor 集成测试（真实 PG）。

T11 验证 _policiesFor 从 DB-backed registry 读取 data_layers，
生成显式分层策略（与运行时分层校验对齐）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL（5433/qa_metadata_test），
pgSession fixture 每测试 TRUNCATE 隔离；TOOL_SEEDS seed + warmUp 前置。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import AgentDefinition
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.tests import _pg_support
from scripts.seed_agent_tool_configs import seedAgentToolConfigs
from scripts.seed_agents import _policiesFor


@pytest.fixture()
async def pgSession() -> AsyncIterator[AsyncSession]:
    """真实 PG 会话 + seed 工具元数据 + warmUp registry。"""
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            await seedAgentToolConfigs(session)
            await session.commit()
            agent_tool_config_registry.invalidate()
            await agent_tool_config_registry.warmUp(session)
            yield session
    finally:
        agent_tool_config_registry.invalidate()
        await engine.dispose()


@pytest.mark.asyncio
async def test_policiesFor_uses_registry_data_layers(pgSession: AsyncSession) -> None:
    """SUPPLIER_360_AGENT → 显式分层策略，DIM + FEATURE 各一条 READ。"""
    # 先注册 SUPPLIER_360_AGENT（tool_name 写到 agent_definition）
    from scripts.seed_agents import _SEED_ACTOR, AGENT_SEEDS, seedAgents

    await seedAgents(pgSession)
    await pgSession.commit()

    policies = await _policiesFor(pgSession, "SUPPLIER_360_AGENT")
    layers = {p.data_layer for p in policies}
    assert layers == {"DIM", "FEATURE"}
    # 数据对象归一化（与工具 data_object 一致）
    assert all(p.data_object == "SUPPLIER" for p in policies)
    assert all(p.permission.value == "read" for p in policies)


@pytest.mark.asyncio
async def test_policiesFor_graph_agent_uses_dim_and_dwd(
    pgSession: AsyncSession,
) -> None:
    """GRAPH_REASONING_AGENT → 显式分层，DIM + DWD（graph_traverse 声明）。"""
    from scripts.seed_agents import seedAgents

    await seedAgents(pgSession)
    await pgSession.commit()

    policies = await _policiesFor(pgSession, "GRAPH_REASONING_AGENT")
    assert {p.data_layer for p in policies} == {"DIM", "DWD"}


@pytest.mark.asyncio
async def test_policiesFor_metadata_agent_uses_default(pgSession: AsyncSession) -> None:
    """元数据 Agent（PROCUREMENT_COPILOT，AGENT_DEFAULT_BINDINGS 无 key）→ 通配 None。"""
    from scripts.seed_agents import seedAgents

    await seedAgents(pgSession)
    await pgSession.commit()

    policies = await _policiesFor(pgSession, "PROCUREMENT_COPILOT")
    # 元数据 Agent → _DEFAULT_POLICIES 单条通配
    assert all(p.data_layer is None for p in policies)