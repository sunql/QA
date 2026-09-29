"""integration 测试统一走真实 PostgreSQL + 完整 API 链路（强制规则：Harness/rules/测试规范.md）。

覆盖根 conftest 的 sqlite client/dbSession fixtures：
- client：真实 PG + 每测试 TRUNCATE + buildTestApp，从 HTTP 入口走完整链路
- dbSession：真实 PG 会话（与 client 同一引擎/工厂），供 Arrange 造数 / Assert 验库

每个测试独立引擎并在结束 dispose（pytest-asyncio 每测试独立事件循环，避免 loop 错配）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from neo4j import Driver, GraphDatabase
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


# ---------------------------------------------------------------------------
# Neo4j fixtures（M0 Unified ID：从 conftest_neo4j.py 合并）
# 约定：
#   - 每个测试启动前清空 Class / Property / Metric 三类节点
#   - 不 mock 外部 driver（直接连 qa-neo4j:7687）
#   - 不用 lifespan_context（与 _testapp 启动分离，避免反复重启）
#   - 通过 getSettings() 读凭据，与生产代码走同一配置源
# ---------------------------------------------------------------------------


@pytest.fixture
async def neo4jCleanDriver() -> AsyncIterator[Driver]:
    """返回已连接的 Neo4j driver，测试结束自动清空 Class/Property/Metric。

    用同步 GraphDatabase.driver 仍 async 兼容：driver 仅持有连接池，实际
    session 操作由 pytest-asyncio 在事件循环里调度；session.run 是同步阻塞
    调用，对每测试 < 100 个节点的清仓足够快。
    """
    settings = getSettings()
    driver: Driver = GraphDatabase.driver(
        settings.neo4jUri,
        auth=(settings.neo4jUser, settings.neo4jPassword),
    )
    yield driver
    with driver.session() as session:
        session.run(
            "MATCH (n) WHERE n:Class OR n:Property OR n:Metric DETACH DELETE n"
        )
    driver.close()


@pytest.fixture
async def neo4jSeedClasses(neo4jCleanDriver: Driver) -> AsyncIterator[Driver]:
    """种入 3 个 Class 节点：1 个带 unified_id，2 个不带（待回填）。"""
    with neo4jCleanDriver.session() as session:
        session.run(
            "CREATE (c:Class {unified_id: 'obj:supplier:S001', name: '供应商'})"
        )
        session.run("CREATE (c:Class {id: 100, name: '物料'})")
        session.run("CREATE (c:Class {id: 101, name: '客户'})")
    yield neo4jCleanDriver


@pytest.fixture
def mockEmbeddingService():
    """EmbeddingService 的替身，模拟 generateEmbedding。

    向量是固定常数（不是真实 LLM 输出），但保证相同文本得到相同向量，
    这样 searchDocuments 能用相同 query 命中刚刚 ingest 的文档。
    """
    svc = AsyncMock()

    async def fake_embed(text: str) -> list[float]:
        # 用文本长度作种子，让同一文本生成同一向量（保证 search 能命中）
        seed = sum(ord(c) for c in text) % 100
        return [float(seed) / 100.0 + 0.001 * i for i in range(1024)]

    svc.generateEmbedding = fake_embed
    return svc


@pytest.fixture
def fakeMinio(monkeypatch):
    """假 MinIO：不联网络，只记录调用。

    用 MagicMock 而不是替身函数：本用例只关心「写没写、写的是不是原始
    字节」，不该顺带把 putSourceObject 对 SDK 的调用形状（位置参还是
    关键字参）钉成测试契约 —— 那是 Task 4 单测的职责。
    """
    fake = MagicMock()
    fake.bucket_exists.return_value = True
    monkeypatch.setattr(
        "app.infrastructure.object_storage._getClient", lambda: fake
    )
    return fake


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


# ---------------------------------------------------------------------------
# Milvus fixtures（M0 Unified ID：3-collection 重构）
# 约定：
#   - sync fixtures（Milvus client 本身是 sync API，不是 asyncio）
#   - 真实 Milvus 容器 qa-milvus:19530，不 mock
#   - embedding 维度恒为 1024（_DIM），与 bge-m3 模型一致
#   - Task M2 落地后需扩展 _dropOntologyCollections 以 drop 3 个新集合
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 1024


def _dropOntologyCollections() -> None:
    """删除全部已知本体 Milvus 集合（idempotent；集合不存在 no-op）。

    Drops ontology_embeddings (old) AND the 3 new type-specific collections
    (ontology_class_embeddings / ontology_property_embeddings /
    ontology_metric_embeddings) added in M2.
    """
    from app.infrastructure.milvus_client import (
        _connAlias,
        _connect,
        dropCollection,
        ensureClassCollection,
        ensureMetricCollection,
        ensurePropertyCollection,
    )
    from pymilvus import Collection, utility

    _connect()

    # Old single collection
    try:
        dropCollection()  # drops _COLLECTION_NAME = "ontology_embeddings"
    except Exception:
        pass  # idempotent

    # 3 new collections (added in M2): drop then recreate (empty) so
    # reconcile() can query them even in a "clean" state.
    for name in (
        "ontology_class_embeddings",
        "ontology_property_embeddings",
        "ontology_metric_embeddings",
    ):
        try:
            if utility.has_collection(name, using=_connAlias()):
                Collection(name, using=_connAlias()).drop()
        except Exception:
            pass  # idempotent

    # Recreate empty collections so reconcile() can query them.
    ensureClassCollection()
    ensurePropertyCollection()
    ensureMetricCollection()


@pytest.fixture
def milvusCleanClient():
    """真实 Milvus 清空夹具：测试前后各 drop 一次 ontology_embeddings。

    后置 drop 保证下一轮测试拿到干净状态。
    """
    _dropOntologyCollections()
    yield
    _dropOntologyCollections()


@pytest.fixture
def milvusSeedOntology(milvusCleanClient):
    """种入 3 条 ontology 行（type='class'）：供应商 / 物料 / 客户。"""
    from app.infrastructure.milvus_client import insertEmbeddings

    insertEmbeddings([
        {"ontology_id": 1001, "type": "class", "name": "供应商", "alias": "supplier", "description": "", "embedding": [0.0] * _EMBEDDING_DIM},
        {"ontology_id": 1002, "type": "class", "name": "物料", "alias": "material", "description": "", "embedding": [0.0] * _EMBEDDING_DIM},
        {"ontology_id": 1003, "type": "class", "name": "客户", "alias": "customer", "description": "", "embedding": [0.0] * _EMBEDDING_DIM},
    ])
    return {"seeded": 3}


@pytest.fixture
def milvusSeedAllTypes(milvusCleanClient):
    """种入 3 条跨类型 ontology 行：1 class + 1 property + 1 metric。

    用于验证类型过滤（typeFilter=class/property/metric）的检索收敛。
    """
    from app.infrastructure.milvus_client import insertEmbeddings

    insertEmbeddings([
        {"ontology_id": 2001, "type": "class", "name": "Class1", "alias": "", "description": "", "embedding": [0.1] * _EMBEDDING_DIM},
        {"ontology_id": 2002, "type": "property", "name": "Property1", "alias": "", "description": "", "embedding": [0.2] * _EMBEDDING_DIM},
        {"ontology_id": 2003, "type": "metric", "name": "Metric1", "alias": "", "description": "", "embedding": [0.3] * _EMBEDDING_DIM},
    ])
    return {"seeded": 3}


@pytest.fixture
def milvusSeedExternalId(milvusCleanClient):
    """种入 3 条 ontology 行（1 class + 1 property + 1 metric），每行 external_id
    已预填为可解析的 unified_id 形式，方便 reconcile 校验。

    Reuses insertEmbeddingsDual (Task M3) which writes to BOTH old + new collections.
    The new collections' external_id fields carry the unified_id values used for
    reconcile matching.
    """
    from app.infrastructure.milvus_client import insertEmbeddingsDual

    insertEmbeddingsDual([
        {"ontology_id": 5001, "type": "class", "name": "ClassS001",
         "alias": "", "description": "",
         "embedding": [0.5] * _EMBEDDING_DIM,
         "external_id": "obj:class:5001"},
        {"ontology_id": 5002, "type": "property", "name": "PropP001",
         "alias": "", "description": "",
         "embedding": [0.6] * _EMBEDDING_DIM,
         "external_id": "obj:property:5002"},
        {"ontology_id": 5003, "type": "metric", "name": "MetricM001",
         "alias": "", "description": "",
         "embedding": [0.7] * _EMBEDDING_DIM,
         "external_id": "obj:metric:5003"},
    ])
    return {"seeded": 3, "external_ids": ["obj:class:5001", "obj:property:5002", "obj:metric:5003"]}
