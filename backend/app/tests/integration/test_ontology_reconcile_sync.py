"""本体对账同步集成测试（真实 PG + 完整 API 链路）。

覆盖两个对账入口：
1. POST /ontology/embeddings/sync-missing —— 类 + 属性 + 指标向量对账
   （M0-P0.4 升级为 3-collection 后，syncMissing 需覆盖 metric；
   M0-P0.4 之前只补类，属性/指标向量缺口只能靠 scripts/backfill_milvus_embeddings.py --cleanup 手工收敛）
2. POST /ontology/graph/sync-missing —— Neo4j 图谱对账（直写 PG 的脚本/批量
   导入失败重试会留下缺图节点/边，此入口按 PG 真源补齐 Class/Property/HAS_PROPERTY/
   REFERENCES/JOIN/语义关系边）

外部依赖 mock 边界：Milvus（模块级 listEmbeddingsAcross3Collections / insertEmbeddingsDual /
deleteByOntologyIdDual，M0-P0.4 升级 3-collection 后已替换原 listAllEmbeddings/insertEmbeddings）、
Neo4j（模块级读写函数）、embedding（注入假服务）；数据层全走真实 PostgreSQL
（Harness/rules/测试规范.md）。
"""

from __future__ import annotations

from typing import Any

import pytest

import app.services.ontology_service as ontology_service_module
from app.dependencies import CurrentUser
from app.domain.enums import ClassRelationType
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyJoinCreate,
    OntologyPropertyCreate,
    OntologyRelationCreate,
)
from app.services.acl_service import ADMIN_ROLE
from app.services.id_mapping_service import IdMappingService
from app.services.ontology_service import OntologyService


_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


async def _uid(dbSession, businessObject: str, pgId: int) -> str:
    """PG id → unified_id（走 id_mapping，与生产同一条解析路径）。

    图侧集合（getClassIds / getJoinPairs / getRelationTriples）返回的都是
    unified_id 字符串，喂假替身时必须用同一类型 —— 早期版本这里给的是 PG int，
    两边异类做差集恒等于全集，测试因此「假绿」放过了真实缺陷。
    """
    row = await IdMappingService().resolveByExternal(dbSession, businessObject, str(pgId))
    assert row is not None, f"{businessObject}:{pgId} 未注册 unified_id"
    return row.unified_id


class _FakeEmbedding:
    """假 embedding 服务：可注入失败文本子串。"""

    def __init__(self, failOn: tuple[str, ...] = ()) -> None:
        self.failOn = failOn
        self.texts: list[str] = []

    async def generateEmbedding(self, text: str) -> list[float]:
        if any(marker in text for marker in self.failOn):
            raise RuntimeError(f"embedding 模拟失败: {text}")
        self.texts.append(text)
        return [0.01 * len(text)] * 4


class _FakeMilvus:
    """假 Milvus：可预设已存在向量行，记录整批插入 + 单条删除/插入。

    M0-P0.4 升级 3-collection 后，生产代码统一走 type-routed API：
    listEmbeddingsAcross3Collections / insertEmbeddingsDual / deleteByOntologyIdDual。
    旧 listAllEmbeddings / insertEmbeddings 已废弃，syncMissing 链路不再调用。
    """

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.existing = existing or []
        self.inserted: list[dict[str, Any]] = []
        self.deleted: list[tuple[int, str]] = []  # (ontology_id, type)

    def listEmbeddingsAcross3Collections(self) -> list[dict[str, Any]]:
        """返回 3 collection 全量合并行（含 type 字段，type-routed 路由依据）。"""
        return list(self.existing)

    def insertEmbeddingsDual(self, records: list[dict[str, Any]]) -> None:
        """记录整批 type-routed 插入（生产路由到对应 collection）。"""
        self.inserted.extend(records)

    def deleteByOntologyIdDual(self, ontologyId: int, type: str) -> None:
        """记录删除（生产在 type-routed collection 上按 ontology_id 删除）。"""
        self.deleted.append((ontologyId, type))
        # 同步维护 existing（与生产 collection 状态一致）
        self.existing = [
            r for r in self.existing
            if not (r.get("ontology_id") == ontologyId and r.get("type") == type)
        ]


class _FakeNeo4j:
    """假 Neo4j：可预设已存在节点/边，记录全部写入调用。

    四个「已存在」集合的元素类型**必须与生产一致 —— 都是 unified_id 字符串**
    （生产按 `COALESCE(unified_id, toString(id))` 读出）。早期版本这里写成
    `set[int]`，把「两边都是 PG id」这个错误前提钉进了替身，于是 int/str 差集的
    真实缺陷在测试里永远看不见（假替身保真度问题）。
    """

    def __init__(
        self,
        classIds: set[str] | None = None,
        propertyIds: set[str] | None = None,
        joinPairs: set[tuple[str, str]] | None = None,
        relationTriples: set[tuple[str, str, str]] | None = None,
    ) -> None:
        self.classIds = classIds or set()
        self.propertyIds = propertyIds or set()
        self.joinPairs = joinPairs or set()
        self.relationTriples = relationTriples or set()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def getClassIds(self) -> set[str]:
        return set(self.classIds)

    def getPropertyIds(self) -> set[str]:
        return set(self.propertyIds)

    def getJoinPairs(self) -> set[tuple[str, str]]:
        return set(self.joinPairs)

    def getRelationTriples(self) -> set[tuple[str, str, str]]:
        return set(self.relationTriples)

    def upsertClassNode(
        self,
        unified_id: str,
        name: str,
        alias: str | None,
        description: str | None,
        sourceTable: str | None,
    ) -> None:
        """按**真签名**记录：生产按关键字调用，`*args` 会把它记成空元组。

        显式参数名还顺带成了契约检查 —— 调用方写成 `id=` 之类会 TypeError。
        """
        self.calls.append(("upsertClassNode", (unified_id,)))

    def reconcileClassSubclassOf(self, classUid: str, parentUid: str) -> None:
        self.calls.append(("reconcileClassSubclassOf", (classUid, parentUid)))

    def upsertPropertyNode(
        self,
        unified_id: str,
        name: str,
        alias: str | None,
        dataType: str | None,
        sourceColumn: str | None,
        isPrimaryKey: bool,
        isForeignKey: bool,
    ) -> None:
        self.calls.append(("upsertPropertyNode", (unified_id,)))

    def linkClassHasProperty(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkClassHasProperty", args))

    def linkPropertyReferences(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkPropertyReferences", args))

    def linkClassJoin(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkClassJoin", args))

    def linkClassRelation(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkClassRelation", args))

    def upsertMetricNode(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("upsertMetricNode", args))

    def linkMetricDerivedFrom(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkMetricDerivedFrom", args))

    def reconcileMetricDerivedFrom(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("reconcileMetricDerivedFrom", args))


def _installFakes(
    monkeypatch: pytest.MonkeyPatch,
    milvus: _FakeMilvus,
    neo4j: _FakeNeo4j,
    embedding: _FakeEmbedding | None = None,
) -> None:
    monkeypatch.setattr(ontology_service_module, "milvus", milvus)
    monkeypatch.setattr(ontology_service_module, "neo4j", neo4j)
    if embedding is not None:
        monkeypatch.setattr(
            ontology_service_module, "EmbeddingService", lambda: embedding
        )
        # 清空已有 OntologyService 实例的 _embedding 缓存：_ensureEmbedding 是
        # 一次性缓存（首次调用 new 一个真实实例缓存在 self._embedding），即使后
        # 续 monkeypatch 替换 EmbeddingService 符号，旧缓存仍生效。CRUD 路径
        # （createProperty / createMetric / updateMetric）触发 best-effort 后台
        # 同步可能在 _installFakes 之前就完成缓存，所以必须强制清零。
        monkeypatch.setattr(
            ontology_service_module.OntologyService,
            "_ensureEmbedding",
            lambda self: embedding,
            raising=True,
        )


async def _seedGraph(
    dbSession, service: OntologyService
) -> dict[str, int]:
    """造 2 类 + 1 属性(带引用) + 1 关联 + 1 语义关系，返回关键 id。"""
    clsA = await service.createClass(
        dbSession,
        OntologyClassCreate(class_name="PRECEIPT", source_table="ODS.PRECEIPT"),
        actor=_ADMIN.userId,
        actor_departments=None,
        sync_embedding=False,
    )
    clsB = await service.createClass(
        dbSession,
        OntologyClassCreate(class_name="SUPPLIER", source_table="ODS.BPSUPPLIER"),
        actor=_ADMIN.userId,
        actor_departments=None,
        sync_embedding=False,
    )
    prop = await service.createProperty(
        dbSession,
        OntologyPropertyCreate(
            class_id=clsA.id,
            property_name="RCP_AMT",
            property_alias="收货金额",
            data_type="DECIMAL",
            source_column="RCP_AMT",
            business_aliases=["金额"],
            description="收货金额",
            ref_class_id=clsB.id,
        ),
        actor=_ADMIN.userId,
        actor_departments=None,
    )
    join = await service.createJoin(
        dbSession,
        OntologyJoinCreate(
            source_class_id=clsA.id,
            source_columns=["RCP_AMT"],
            target_class_id=clsB.id,
            target_columns=["S_CODE"],
            join_type="INNER",
        ),
        actor=_ADMIN.userId,
    )
    relation = await service.createRelation(
        dbSession,
        OntologyRelationCreate(
            source_class_id=clsA.id,
            target_class_id=clsB.id,
            relation_type=ClassRelationType.SUPPLIES,
        ),
        actor=_ADMIN.userId,
    )
    return {
        "classA": clsA.id,
        "classB": clsB.id,
        "prop": prop.id,
        "join": join.id,
        "relation": relation.id,
    }


class TestEmbeddingSyncCoversProperties:
    async def test_syncs_missing_class_and_property_vectors(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """类与属性向量都缺 → 两类都补；属性向量文本含 名称+别名+描述。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        milvus = _FakeMilvus()
        embedding = _FakeEmbedding()
        _installFakes(monkeypatch, milvus, _FakeNeo4j(), embedding)

        result = await service.syncMissingClassEmbeddings(dbSession)

        assert result["totalClasses"] == 2
        assert result["missingCount"] == 2
        # syncedCount = 类 + 属性总同步条数
        assert result["syncedCount"] == 3
        assert result["totalProperties"] == 1
        assert result["missingPropertyCount"] == 1
        assert result["syncedPropertyCount"] == 1
        assert len(milvus.inserted) == 3
        types = sorted(r["type"] for r in milvus.inserted)
        assert types == ["class", "class", "property"]
        propRow = next(r for r in milvus.inserted if r["type"] == "property")
        assert propRow["ontology_id"] == ids["prop"]
        assert propRow["alias"] == "收货金额"
        assert propRow["description"] == "收货金额"
        # 属性向量文本口径 = property_name + business_aliases + description
        assert any("RCP_AMT" in t and "收货金额" in t for t in embedding.texts)

    async def test_present_vectors_skipped(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """类与属性向量齐全 → 全部 skip，不触发插入。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        existing = [
            {"ontology_id": ids["classA"], "type": "class"},
            {"ontology_id": ids["classB"], "type": "class"},
            {"ontology_id": ids["prop"], "type": "property"},
        ]
        milvus = _FakeMilvus(existing=existing)
        _installFakes(monkeypatch, milvus, _FakeNeo4j())

        result = await service.syncMissingClassEmbeddings(dbSession)

        assert result["missingCount"] == 0
        assert result["missingPropertyCount"] == 0
        assert milvus.inserted == []

    async def test_property_embedding_failure_isolated(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """属性向量生成失败不中断：类照常补齐，失败聚合进 propertyFailures。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        milvus = _FakeMilvus()
        embedding = _FakeEmbedding(failOn=("RCP_AMT",))
        _installFakes(monkeypatch, milvus, _FakeNeo4j(), embedding)

        result = await service.syncMissingClassEmbeddings(dbSession)

        assert result["syncedCount"] == 2  # 两个类照常
        assert result["syncedPropertyCount"] == 0
        assert result["failedPropertyCount"] == 1
        assert result["propertyFailures"][0]["propertyId"] == ids["prop"]
        assert [r["type"] for r in milvus.inserted] == ["class", "class"]


class TestCreateCrudSyncsEmbedding:
    """CRUD 路径自动同步：M0-P0.4 后 createProperty/createMetric/updateMetric 应
    即时落 Milvus（无需等 batch sync）。createClass/updateClass 长期 OK；createProperty
    漏调 helper；metric 全缺。"""

    async def test_create_property_syncs_embedding_immediately(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """createProperty 后属性向量立即出现在 Milvus（type=property，name/alias/description 全带）。"""
        from app.domain.schemas import OntologyClassCreate, OntologyPropertyCreate

        service = OntologyService()
        milvus = _FakeMilvus()
        embedding = _FakeEmbedding()
        _installFakes(monkeypatch, milvus, _FakeNeo4j(), embedding)

        cls = await service.createClass(
            dbSession,
            OntologyClassCreate(class_name="GOODS", source_table="ODS.GOODS"),
            actor=_ADMIN.userId,
            actor_departments=None,
            sync_embedding=False,  # 隔离类向量，专注属性同步
        )
        # 等异步 background task 完成（_PENDING_SYNC_TASKS 由 helper 持有）
        await _waitForPendingSyncTasks()

        milvus_before_prop = list(milvus.inserted)
        prop = await service.createProperty(
            dbSession,
            OntologyPropertyCreate(
                class_id=cls.id,
                property_name="QTY",
                property_alias="数量",
                data_type="DECIMAL",
                source_column="QTY",
                business_aliases=["qty"],
                description="收货数量",
            ),
            actor=_ADMIN.userId,
            actor_departments=None,
        )
        await _waitForPendingSyncTasks()

        propRows = [r for r in milvus.inserted if r.get("type") == "property" and r.get("ontology_id") == prop.id]
        assert len(propRows) == 1, f"createProperty 未触发属性向量同步，inserted={milvus.inserted[len(milvus_before_prop):]}"
        assert propRows[0]["name"] == "QTY"
        assert propRows[0]["alias"] == "数量"
        assert propRows[0]["description"] == "收货数量"
        assert any("QTY" in t and "数量" in t for t in embedding.texts)

    async def test_create_metric_syncs_embedding_immediately(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """createMetric 后指标向量立即出现在 Milvus（type=metric，公式文本进入 embedding）。"""
        from app.domain.schemas import OntologyMetricCreate

        service = OntologyService()
        milvus = _FakeMilvus()
        embedding = _FakeEmbedding()
        _installFakes(monkeypatch, milvus, _FakeNeo4j(), embedding)

        metric = await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="po_total_qty",
                metric_alias="采购订单总数量",
                agg_function="SUM",
                formula="SUM(quantity)",
            ),
            actor=_ADMIN.userId,
            actor_departments=None,
        )
        await _waitForPendingSyncTasks()

        metricRows = [r for r in milvus.inserted if r.get("type") == "metric" and r.get("ontology_id") == metric.id]
        assert len(metricRows) == 1, f"createMetric 未触发指标向量同步，inserted={milvus.inserted}"
        assert metricRows[0]["name"] == "po_total_qty"
        assert metricRows[0]["alias"] == "采购订单总数量"
        # embedding 文本应含公式
        assert any("po_total_qty" in t and "SUM(quantity)" in t for t in embedding.texts)

    async def test_update_metric_refreshes_embedding(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """updateMetric 后指标向量应被先删后插（幂等覆盖），文本带新公式。"""
        from app.domain.schemas import OntologyMetricCreate, OntologyMetricUpdate

        service = OntologyService()
        milvus = _FakeMilvus()
        embedding = _FakeEmbedding()
        _installFakes(monkeypatch, milvus, _FakeNeo4j(), embedding)

        metric = await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="metric_refresh_test",
                metric_alias="刷新测试",
                agg_function="SUM",
                formula="SUM(a)",
            ),
            actor=_ADMIN.userId,
            actor_departments=None,
        )
        await _waitForPendingSyncTasks()
        inserted_before = len(milvus.inserted)
        deleted_before = list(milvus.deleted)

        await service.updateMetric(
            dbSession,
            metric.id,
            OntologyMetricUpdate(formula="SUM(b)"),
            actor=_ADMIN.userId,
            actor_departments=None,
        )
        await _waitForPendingSyncTasks()

        # updateMetric 应先 deleteByOntologyIdDual 再 insertEmbeddingsDual
        assert (metric.id, "metric") in milvus.deleted, (
            f"updateMetric 未删除旧向量，deleted={milvus.deleted[len(deleted_before):]}"
        )
        newMetricRows = [
            r for r in milvus.inserted[inserted_before:]
            if r.get("type") == "metric" and r.get("ontology_id") == metric.id
        ]
        assert len(newMetricRows) == 1
        # 新公式应进入 embedding 文本
        assert any("SUM(b)" in t for t in embedding.texts[len(embedding.texts) // 2:])


class TestSyncMissingIncludesMetrics:
    """syncMissingClassEmbeddings 扩展：覆盖 metric 类型（M0-P0.4 后 metric 也走 3-collection）。"""

    async def test_syncs_missing_metric_vectors(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """metric 向量缺失 → 自动补齐，type=metric 行进入 insertEmbeddingsDual。"""
        from app.domain.schemas import OntologyMetricCreate

        service = OntologyService()
        # fake 在 createMetric **前**就装好：M0-P0.4 修复后 createMetric 也会自动
        # 触发 best-effort 向量同步，不装 fake 会撞真 embedding provider。
        milvus = _FakeMilvus()
        embedding = _FakeEmbedding()
        _installFakes(monkeypatch, milvus, _FakeNeo4j(), embedding)

        # 先种 2 个 metric（首次同步走 _syncMetricEmbeddingBestEffort 后台任务）
        m1 = await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="metric_a",
                metric_alias="指标A",
                agg_function="SUM",
                formula="SUM(x)",
            ),
            actor=_ADMIN.userId,
            actor_departments=None,
        )
        m2 = await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="metric_b",
                metric_alias="指标B",
                agg_function="COUNT",
                formula="COUNT(*)",
            ),
            actor=_ADMIN.userId,
            actor_departments=None,
        )
        await _waitForPendingSyncTasks()

        # 重置 fake Milvus 状态：清空已写入向量，模拟 metric 漏同步（覆盖
        # createMetric 最佳努力可能失败的场景，验 syncMissing 兜底路径）
        milvus.existing = []
        milvus.inserted = []
        milvus.deleted = []

        result = await service.syncMissingClassEmbeddings(dbSession)

        assert result["totalMetrics"] == 2
        assert result["missingMetricCount"] == 2
        assert result["syncedMetricCount"] == 2
        assert result["failedMetricCount"] == 0
        types = sorted(r["type"] for r in milvus.inserted)
        assert "metric" in types, f"syncMissing 未补 metric 向量，types={types}"


async def _waitForPendingSyncTasks(timeoutSec: float = 5.0) -> None:
    """等待 ontology_service 模块级 _PENDING_SYNC_TASKS 集合中所有任务完成。

    CRUD 同步路径走 _syncClassEmbeddingBestEffort / _syncPropertyEmbeddingBestEffort /
    _syncMetricEmbeddingBestEffort 等后台 asyncio.create_task（避免拖慢 CRUD 响应）；
    测试断言前必须等任务落库，否则 milvus.inserted 为空。
    """
    import asyncio

    import app.services.ontology_service as svc

    deadline = asyncio.get_event_loop().time() + timeoutSec
    while svc._PENDING_SYNC_TASKS:
        remaining = list(svc._PENDING_SYNC_TASKS)
        await asyncio.gather(*remaining, return_exceptions=True)
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"sync 后台任务超时未完成，pending={remaining}"
            )
        if not svc._PENDING_SYNC_TASKS:
            break


class TestGraphSyncMissing:
    async def test_backfills_missing_graph_entities(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """图库为空 → 类/属性/HAS_PROPERTY/REFERENCES/JOIN/语义关系全部补齐。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        uidA = await _uid(dbSession, "CLASS", ids["classA"])
        uidB = await _uid(dbSession, "CLASS", ids["classB"])
        uidProp = await _uid(dbSession, "PROPERTY", ids["prop"])
        neo4j = _FakeNeo4j()
        _installFakes(monkeypatch, _FakeMilvus(), neo4j)

        result = await service.syncMissingGraph(dbSession)

        assert result["totalClasses"] == 2
        assert result["missingClassCount"] == 2
        assert result["syncedClassCount"] == 2
        assert result["missingPropertyCount"] == 1
        assert result["syncedPropertyCount"] == 1
        assert result["missingJoinCount"] == 1
        assert result["syncedJoinCount"] == 1
        assert result["missingRelationCount"] == 1
        assert result["syncedRelationCount"] == 1
        assert result["failedCount"] == 0

        upsertIds = [c[1][0] for c in neo4j.calls if c[0] == "upsertClassNode"]
        assert sorted(upsertIds) == sorted([uidA, uidB])
        assert ("linkClassHasProperty", (uidA, uidProp)) in neo4j.calls
        assert ("linkPropertyReferences", (uidProp, uidB)) in neo4j.calls
        assert ("linkClassJoin", (uidA, uidB)) in neo4j.calls
        assert (
            "linkClassRelation",
            (uidA, uidB, ClassRelationType.SUPPLIES.value),
        ) in neo4j.calls

    async def test_skips_existing_graph_entities(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """图库齐全 → 全部 skip，零写入。

        这是**幂等性**的回归闸：若差分两边类型不一致（PG id 减 unified_id），
        差集恒等于全集 ⇒ 这里会看到 missingJoinCount=1、neo4j.calls 非空。
        """
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        uidA = await _uid(dbSession, "CLASS", ids["classA"])
        uidB = await _uid(dbSession, "CLASS", ids["classB"])
        uidProp = await _uid(dbSession, "PROPERTY", ids["prop"])
        neo4j = _FakeNeo4j(
            classIds={uidA, uidB},
            propertyIds={uidProp},
            joinPairs={(uidA, uidB)},
            relationTriples={(uidA, uidB, "SUPPLIES")},
        )
        _installFakes(monkeypatch, _FakeMilvus(), neo4j)

        result = await service.syncMissingGraph(dbSession)

        assert result["missingClassCount"] == 0
        assert result["missingPropertyCount"] == 0
        assert result["missingJoinCount"] == 0
        assert result["missingRelationCount"] == 0
        assert neo4j.calls == []

    async def test_relation_link_failure_isolated(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单条语义关系入图失败不中断其余补齐，聚合进 failures。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        neo4j = _FakeNeo4j()

        def _boom(*args: Any, **kw: Any) -> None:
            raise RuntimeError("neo4j 不可达")

        neo4j.linkClassRelation = _boom  # type: ignore[method-assign]
        _installFakes(monkeypatch, _FakeMilvus(), neo4j)

        result = await service.syncMissingGraph(dbSession)

        assert result["syncedRelationCount"] == 0
        assert result["failedCount"] == 1
        assert result["failures"][0]["entityType"] == "relation"
        assert result["failures"][0]["entityId"] == ids["relation"]
        # 其余实体照常补齐
        assert result["syncedClassCount"] == 2
        assert result["syncedPropertyCount"] == 1
        assert result["syncedJoinCount"] == 1


class TestReconcileEndpoints:
    async def test_api_sync_missing_embeddings_returns_property_fields(
        self, client, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """API 层：sync-missing 响应携带类+属性两组计数字段。"""
        import app.api.v1.ontology as ontology_api

        service = ontology_service_module.OntologyService()
        await _seedGraph(dbSession, service)
        _installFakes(monkeypatch, _FakeMilvus(), _FakeNeo4j())
        monkeypatch.setattr(
            ontology_api._ontologyService, "_embedding", _FakeEmbedding()
        )

        resp = await client.post("/api/v1/ontology/embeddings/sync-missing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalClasses"] == 2
        assert body["totalProperties"] == 1
        assert body["syncedPropertyCount"] == 1

    async def test_api_graph_sync_missing(
        self, client, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """API 层：graph/sync-missing 走完整链路返回对账摘要。"""
        service = ontology_service_module.OntologyService()
        await _seedGraph(dbSession, service)
        _installFakes(monkeypatch, _FakeMilvus(), _FakeNeo4j())

        resp = await client.post("/api/v1/ontology/graph/sync-missing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["missingClassCount"] == 2
        assert body["syncedClassCount"] == 2
        assert body["missingPropertyCount"] == 1
        assert body["failedCount"] == 0
