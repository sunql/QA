"""本体对账同步集成测试（真实 PG + 完整 API 链路）。

覆盖两个对账入口：
1. POST /ontology/embeddings/sync-missing —— 类 + 属性向量对账（此前只补类，
   属性向量缺口只能靠 scripts/backfill_milvus_embeddings.py --cleanup 手工收敛）
2. POST /ontology/graph/sync-missing —— Neo4j 图谱对账（直写 PG 的脚本/批量
   导入失败重试会留下缺图节点/边，此入口按 PG 真源补齐 Class/Property/HAS_PROPERTY/
   REFERENCES/JOIN/语义关系边）

外部依赖 mock 边界：Milvus（模块级 listAllEmbeddings/insertEmbeddings）、
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
from app.services.ontology_service import OntologyService


_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


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
    """假 Milvus：可预设已存在向量行，记录整批插入。"""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.existing = existing or []
        self.inserted: list[dict[str, Any]] = []

    def listAllEmbeddings(self) -> list[dict[str, Any]]:
        return list(self.existing)

    def insertEmbeddings(self, records: list[dict[str, Any]]) -> None:
        self.inserted.extend(records)


class _FakeNeo4j:
    """假 Neo4j：可预设已存在节点/边，记录全部写入调用。"""

    def __init__(
        self,
        classIds: set[int] | None = None,
        propertyIds: set[int] | None = None,
        joinPairs: set[tuple[int, int]] | None = None,
        relationTriples: set[tuple[int, int, str]] | None = None,
    ) -> None:
        self.classIds = classIds or set()
        self.propertyIds = propertyIds or set()
        self.joinPairs = joinPairs or set()
        self.relationTriples = relationTriples or set()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def getClassIds(self) -> set[int]:
        return set(self.classIds)

    def getPropertyIds(self) -> set[int]:
        return set(self.propertyIds)

    def getJoinPairs(self) -> set[tuple[int, int]]:
        return set(self.joinPairs)

    def getRelationTriples(self) -> set[tuple[int, int, str]]:
        return set(self.relationTriples)

    def upsertClassNode(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("upsertClassNode", args))

    def reconcileClassSubclassOf(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("reconcileClassSubclassOf", args))

    def upsertPropertyNode(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("upsertPropertyNode", args))

    def linkClassHasProperty(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkClassHasProperty", args))

    def linkPropertyReferences(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkPropertyReferences", args))

    def linkClassJoin(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkClassJoin", args))

    def linkClassRelation(self, *args: Any, **kw: Any) -> None:
        self.calls.append(("linkClassRelation", args))


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


class TestGraphSyncMissing:
    async def test_backfills_missing_graph_entities(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """图库为空 → 类/属性/HAS_PROPERTY/REFERENCES/JOIN/语义关系全部补齐。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
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
        assert sorted(upsertIds) == sorted([ids["classA"], ids["classB"]])
        assert ("linkClassHasProperty", (ids["classA"], ids["prop"])) in neo4j.calls
        assert ("linkPropertyReferences", (ids["prop"], ids["classB"])) in neo4j.calls
        assert ("linkClassJoin", (ids["classA"], ids["classB"])) in neo4j.calls
        assert (
            "linkClassRelation",
            (ids["classA"], ids["classB"], ClassRelationType.SUPPLIES.value),
        ) in neo4j.calls

    async def test_skips_existing_graph_entities(
        self, dbSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """图库齐全 → 全部 skip，零写入。"""
        service = OntologyService()
        ids = await _seedGraph(dbSession, service)
        neo4j = _FakeNeo4j(
            classIds={ids["classA"], ids["classB"]},
            propertyIds={ids["prop"]},
            joinPairs={(ids["classA"], ids["classB"])},
            relationTriples={(ids["classA"], ids["classB"], "SUPPLIES")},
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
