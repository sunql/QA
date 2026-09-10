"""integration 测试统一走真实 PostgreSQL + 完整 API 链路（强制规则：Harness/rules/测试规范.md）。

覆盖根 conftest 的 sqlite client/dbSession fixtures：
- client：真实 PG + 每测试 TRUNCATE + buildTestApp，从 HTTP 入口走完整链路
- dbSession：真实 PG 会话（与 client 同一引擎/工厂），供 Arrange 造数 / Assert 验库

每个测试独立引擎并在结束 dispose（pytest-asyncio 每测试独立事件循环，避免 loop 错配）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.infrastructure import database as dbModule
from app.infrastructure.llm.embedding_provider_factory import resetEmbeddingClientCache
from app.infrastructure.security import crypto
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.tests import _pg_support


@pytest.fixture()
async def client() -> AsyncIterator[AsyncClient]:
    """完整 API 链路客户端：真实 PG（覆盖根 conftest 的 sqlite 内存库 client）。"""
    getSettings.cache_clear()
    crypto.resetFernet()
    resetEmbeddingClientCache()  # embedding provider 缓存跨测试清理（seed 数据会被 TRUNCATE）
    async for ac in _pg_support.pgApiClient():
        yield ac


@pytest.fixture()
async def dbSession(client: AsyncClient) -> AsyncIterator[AsyncSession]:
    """真实 PG 会话：依赖 client 确保 pgApiClient 已替换全局会话工厂。"""
    factory = dbModule.getSessionFactory()
    async with factory() as session:
        yield session


@pytest.fixture(autouse=True)
async def warmAgentCaches(dbSession: AsyncSession) -> AsyncIterator[None]:
    """每个测试前 warmUp agent_binding_cache + agent_tool_config_registry。

    集成测试无 lifespan（TestClient/TestApp 不触发 startup），原 in-memory
    registry 不需要 warmUp；DB-backed registry（feat-agent-tool-config-db）必须显式
    warmUp，否则 runtime 测试会因 'Registry 未 warmUp' 抛 RuntimeError。

    seed_agent_tool_configs（3 个内置工具 upsert）由 lifespan 完成；集成测试
    走 TRUNCATE+seed 路径，先 upsert 再 warmUp。
    """
    from scripts.seed_agent_tool_configs import seedAgentToolConfigs

    await seedAgentToolConfigs(dbSession)
    await dbSession.commit()
    # Seed business_object rows so FK targets exist for entity_mapping /
    # feature_definition / document_entity_relation tests (Task 8 FK constraint).
    from scripts.seed_business_objects import seedBusinessObjects

    await seedBusinessObjects(dbSession)

    agent_binding_cache.invalidate()
    await agent_binding_cache.warmUp(dbSession)
    agent_tool_config_registry.invalidate()
    await agent_tool_config_registry.warmUp(dbSession)
    # warmUp feature_rule_registry（feat-feature-rule-config）：集成测试无
    # lifespan，必须显式 warmUp，否则 runtime 测试会因 'Registry 未 warmUp'
    # 抛 RuntimeError。seedFeatureRules 先行确保运行时测试有规则可评估。
    from scripts.seed_feature_rules import seedFeatureRules
    from app.services.feature_rule_registry import feature_rule_registry

    await seedFeatureRules(dbSession)
    await dbSession.commit()
    feature_rule_registry.invalidate()
    await feature_rule_registry.warmUp(dbSession)
    # warmUp business_object_registry：DB-backed registry，集成测试无 lifespan
    # 必须显式 warmUp，否则 runtime 测试会因 'Registry 未 warmUp' 抛 RuntimeError。
    from app.services.business_object_registry import businessObjectRegistry

    businessObjectRegistry.invalidate()
    await businessObjectRegistry.warmUp(dbSession)
    # Phase 1.4：warmUp kpi_match_cache（L1 匹配依赖）
    from app.services.kpi_match_cache import kpi_match_cache
    kpi_match_cache.onKpiChanged()
    await kpi_match_cache.warmUp(dbSession)
    # 关掉 warmUp SELECT 留下的隐式事务：否则 dbSession 持有 AccessShareLock，
    # 阻塞后续 pgSession/engine B 的 TRUNCATE（feat-agent-tool-config-db 教训）
    await dbSession.commit()
    yield
    agent_binding_cache.invalidate()
    agent_tool_config_registry.invalidate()
    feature_rule_registry.invalidate()
    businessObjectRegistry.invalidate()
    from app.services.kpi_match_cache import kpi_match_cache
    kpi_match_cache.onKpiChanged()
