"""seed_agents._policiesFor 集成测试（真实 PG）。

T11 验证 _policiesFor 从 DB-backed registry 读取 data_layers，
生成显式分层策略（与运行时分层校验对齐）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL（5433/qa_metadata_test），
pgSession fixture 每测试 TRUNCATE 隔离；TOOL_SEEDS seed + warmUp 前置。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import AgentAccessPolicy, AgentDefinition
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


async def _policyRows(session: AsyncSession, agentCode: str) -> set[tuple[str, str | None]]:
    """读该 Agent 的策略行（绕开 identity map：seed 走 service 层，缓存不可信）。"""
    rows = await session.execute(
        select(AgentAccessPolicy.data_object, AgentAccessPolicy.data_layer)
        .join(AgentDefinition, AgentDefinition.id == AgentAccessPolicy.agent_id)
        .where(AgentDefinition.agent_code == agentCode)
    )
    return {(obj, layer) for obj, layer in rows.all()}


@pytest.mark.asyncio
async def test_existing_layerless_agent_is_healed_by_reseed(
    pgSession: AsyncSession,
) -> None:
    """既有层无关 Agent 策略被清空后，重跑 seed 必须把对象粒度策略补回来。

    这是真实故障的回归：`seedAgents` 对已存在的 `agent_code` 直接 `continue`，
    `_policiesFor` 不会再跑；若愈合逻辑又对 `data_layers == []` 的工具早退，
    策略就永远补不回来 —— Agent 行还在、策略没了，运行时精确比对 → 403，
    而 seed 输出一切正常（静默 fail-closed）。测试库 TRUNCATE 后重 seed、
    旧部署新增知识工具，都会踩到。
    """
    from scripts.seed_agents import seedAgents

    await seedAgents(pgSession)
    await pgSession.commit()
    assert await _policyRows(pgSession, "WIKI_SEARCH_AGENT") == {("WIKI_PAGE", None)}

    # 模拟「Agent 行还在、策略已丢」：直接删策略，Agent 本体不动
    agent = (
        await pgSession.execute(
            select(AgentDefinition).where(AgentDefinition.agent_code == "WIKI_SEARCH_AGENT")
        )
    ).scalar_one()
    await pgSession.execute(
        delete(AgentAccessPolicy).where(AgentAccessPolicy.agent_id == agent.id)
    )
    await pgSession.commit()
    # expire_all：listPolicies 读的是 agent.policies **关系集合**（identity map 缓存），
    # 上面的批量 delete 绕过了 ORM 关系，不 expire 会看到幽灵策略 → 愈合静默不触发。
    # 真实故障里删策略的是**另一个进程**（TRUNCATE），会话本就是干净的，故这里 expire
    # 才是忠实模拟，不是给代码打补丁。
    pgSession.expire_all()
    assert await _policyRows(pgSession, "WIKI_SEARCH_AGENT") == set()

    # Act：重跑 seed（Agent 已存在 → 只能靠愈合分支）
    await seedAgents(pgSession)
    await pgSession.commit()

    # Assert：对象粒度 READ 已补齐
    assert await _policyRows(pgSession, "WIKI_SEARCH_AGENT") == {("WIKI_PAGE", None)}


@pytest.mark.asyncio
async def test_heal_is_idempotent_and_leaves_layered_agents_alone(
    pgSession: AsyncSession,
) -> None:
    """愈合幂等（重跑不重复增行），且不动有层工具的策略。"""
    from scripts.seed_agents import seedAgents

    await seedAgents(pgSession)
    await seedAgents(pgSession)
    await pgSession.commit()

    assert await _policyRows(pgSession, "WIKI_SEARCH_AGENT") == {("WIKI_PAGE", None)}
    assert await _policyRows(pgSession, "SUPPLIER_360_AGENT") == {
        ("SUPPLIER", "DIM"),
        ("SUPPLIER", "FEATURE"),
    }