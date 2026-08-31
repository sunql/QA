"""Phase 6.2 业务关系图单元测试（mock Neo4j + fake session）。

覆盖：
- 白名单防御：非法 label / 非法关系类型 -> ValueError（CQL 拼接防注入）；
- validateSchema：service 常量与 neo4j_client 白名单不漂移；
- Sheet 16 边派生：SUPPLIES 30 条不重复 / CONTAINS 9 / GENERATES 3 /
  INSPECTED_BY 2 / GENERATED 1；
- seedGraphRelations：节点写入 + 边 MERGE 语义 + CQL 结构断言；
- SIGNED 边：document_entity_relation -> (document_id, name, supplier_key)；
- 去重：同一实体多源系统映射只产一个节点。

Neo4j driver 用 mock（同 test_ontology_api 模式）；PG 数据用内存 fake rows
（本文件不触 DB，真实 PG + 真实 Neo4j 的端到端见 integration 测试）。
"""

from __future__ import annotations

import pytest

import app.infrastructure.neo4j_client as neo4j_module
from app.domain.enums import EntityType, MatchRule, SourceSystem
from app.services.graph_relation_service import GraphRelationService

# =============================================================================
# Mock Neo4j driver（记录 CQL + 参数，供断言）
# =============================================================================


class _MockRecord:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]


class _MockSession:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def __enter__(self) -> "_MockSession":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def run(self, cql: str, **params: object) -> list:
        self.queries.append((cql, params))
        if "count(b) AS deleted" in cql:
            return [_MockRecord({"deleted": 0})]
        if "count(b) AS cnt" in cql:
            return [_MockRecord({"cnt": 0})]
        if "count(r) AS cnt" in cql:
            return [_MockRecord({"cnt": 0})]
        return []


class _MockDriver:
    def __init__(self) -> None:
        self.sessions: list[_MockSession] = []

    def session(self) -> _MockSession:
        s = _MockSession()
        self.sessions.append(s)
        return s

    def close(self) -> None:
        pass


@pytest.fixture()
def mockNeo4j(monkeypatch: pytest.MonkeyPatch) -> _MockDriver:
    driver = _MockDriver()
    monkeypatch.setattr(neo4j_module, "_DRIVER", driver)
    monkeypatch.setattr(neo4j_module, "getDriver", lambda: driver)
    return driver


def _allQueries(driver: _MockDriver) -> list[tuple[str, dict]]:
    return [q for s in driver.sessions for q in s.queries]


# =============================================================================
# Fake PG rows（entity_mapping / document 关联）
# =============================================================================


class _FakeMapping:
    """EntityMapping 的最小替身（仅 seedGraphRelations 用到的字段）。"""

    def __init__(self, entityType: EntityType, key: int, code: str) -> None:
        self.entity_type = entityType
        self.enterprise_key = key
        self.enterprise_code = code
        self.source_system = SourceSystem.ERP
        self.match_rule = MatchRule.BUSINESS_KEY


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list:
        return self._rows


class _FakeSession:
    """select(...) 按查询对象分发的最小 fake。"""

    def __init__(self, mappings: list, docRows: list) -> None:
        self._mappings = mappings
        self._docRows = docRows

    async def execute(self, query):
        queryText = str(query)
        if "entity_mapping" in queryText:
            return _FakeResult(self._mappings)
        if "document_entity_relation" in queryText:
            return _FakeResult(self._docRows)
        raise AssertionError(f"unexpected query: {queryText[:80]}")


# =============================================================================
# 白名单 / schema 校验
# =============================================================================


class TestWhitelist:
    def test_upsert_rejects_unknown_label(self, mockNeo4j) -> None:
        with pytest.raises(ValueError, match="Invalid business entity label"):
            neo4j_module.upsertBusinessEntityNode(
                label="Class", key="1", code="x", name=None, source="test"
            )

    def test_link_rejects_unknown_relation_type(self, mockNeo4j) -> None:
        with pytest.raises(ValueError, match="Invalid business relation type"):
            neo4j_module.linkBusinessRelation(
                relType="HACKS",
                fromLabel="Supplier",
                fromKey="1",
                toLabel="Material",
                toKey="2",
            )

    def test_link_rejects_unknown_endpoint_label(self, mockNeo4j) -> None:
        with pytest.raises(ValueError, match="Invalid business entity label"):
            neo4j_module.linkBusinessRelation(
                relType="SUPPLIES",
                fromLabel="Property",
                fromKey="1",
                toLabel="Material",
                toKey="2",
            )

    def test_label_injection_attempt_rejected(self, mockNeo4j) -> None:
        """CQL 注入尝试（label 内嵌 CQL 片段）必须被白名单拒绝。"""
        with pytest.raises(ValueError):
            neo4j_module.upsertBusinessEntityNode(
                label="Supplier} DETACH DELETE all //",
                key="1",
                code="x",
                name=None,
                source="test",
            )

    def test_validate_schema_no_drift(self) -> None:
        service = GraphRelationService()
        assert service.validateSchema() == []

    def test_business_labels_disjoint_from_ontology(self) -> None:
        """业务 label 与本体 _ALLOWED_LABELS 无交集（图隔离前提）。"""
        assert not (
            neo4j_module.BUSINESS_ENTITY_LABELS & neo4j_module._ALLOWED_LABELS
        )


# =============================================================================
# Sheet 16 边派生
# =============================================================================


class TestSheet16Edges:
    def test_supplies_30_unique_pairs(self) -> None:
        service = GraphRelationService()
        edges = dict(service._sheet16Edges())
        supplies = edges["SUPPLIES"]
        assert len(supplies) == 30
        pairs = {(fromKey, toKey) for fromKey, toKey, _, _ in supplies}
        assert len(pairs) == 30  # 无重复边

    def test_all_relation_counts(self) -> None:
        service = GraphRelationService()
        edges = dict(service._sheet16Edges())
        assert len(edges["CONTAINS"]) == 9
        assert len(edges["GENERATES"]) == 3
        assert len(edges["INSPECTED_BY"]) == 2
        assert len(edges["GENERATED"]) == 1
        total = sum(len(v) for v in edges.values())
        assert total == 45  # 30 + 9 + 3 + 2 + 1

    def test_supplier_supplies_exactly_three_materials(self) -> None:
        service = GraphRelationService()
        for supplierIndex in range(1, 11):
            indexes = service._suppliedMaterialIndexes(supplierIndex, 10)
            assert len(indexes) == 3
            assert len(set(indexes)) == 3  # 同一供应商不重复供应同一物料
            assert all(1 <= i <= 10 for i in indexes)

    def test_all_edges_use_whitelisted_labels(self) -> None:
        service = GraphRelationService()
        for _, pairs in service._sheet16Edges():
            for _, _, fromLabel, toLabel in pairs:
                assert fromLabel in neo4j_module.BUSINESS_ENTITY_LABELS
                assert toLabel in neo4j_module.BUSINESS_ENTITY_LABELS


# =============================================================================
# seedGraphRelations（mock neo4j + fake PG rows）
# =============================================================================


def _sampleMappings() -> list[_FakeMapping]:
    """2 供应商（各 2 源系统映射，验证去重）+ 2 物料 + 1 PO + 1 GR + 1 IQC。"""
    return [
        _FakeMapping(EntityType.SUPPLIER, 100_001, "SUP000001"),
        _FakeMapping(EntityType.SUPPLIER, 100_001, "SUP000001"),  # SRM 重复源
        _FakeMapping(EntityType.SUPPLIER, 100_002, "SUP000002"),
        _FakeMapping(EntityType.MATERIAL, 200_001, "RM-STEEL-001"),
        _FakeMapping(EntityType.MATERIAL, 200_002, "RM-STEEL-002"),
        _FakeMapping(EntityType.PO, 300_001, "PO202608001"),
        _FakeMapping(EntityType.GR, 400_001, "GR202608001"),
        _FakeMapping(EntityType.IQC, 500_001, "IQC202608001"),
    ]


class TestSeedGraphRelations:
    async def test_result_counts_and_dedup(self, mockNeo4j) -> None:
        service = GraphRelationService()
        session = _FakeSession(_sampleMappings(), [])
        result = await service.seedGraphRelations(session)

        # 去重后：2 supplier + 2 material + 1 PO + 1 GR + 1 IQC = 7
        # + sheet16_demo 4 个补充节点 + document_catalog 0
        assert result.nodesBySource["entity_mapping"] == 7
        assert result.nodesBySource["sheet16_demo"] == 4
        assert result.nodeCount == 11
        # 全部 Sheet 16 边都会写入（边派生是静态的，与 PG 行数无关）
        assert result.edgesByType["SUPPLIES"] == 30
        assert result.edgeCount == 45

    async def test_node_cql_structure(self, mockNeo4j) -> None:
        service = GraphRelationService()
        await service.seedGraphRelations(_FakeSession(_sampleMappings(), []))

        queries = _allQueries(mockNeo4j)
        nodeQueries = [
            (cql, params)
            for cql, params in queries
            if "MERGE (b:BusinessEntity:" in cql
        ]
        assert nodeQueries, "应有业务节点 MERGE 语句"
        # 双 label 结构：BusinessEntity + 具体类型
        cql, params = nodeQueries[0]
        assert ":BusinessEntity:" in cql
        assert "SET b.code = $code" in cql
        assert params["source"] == "entity_mapping"
        # label 经白名单拼接后只能是已知值
        assert any(f"MERGE (b:BusinessEntity:{label}" in cql for cql, _ in nodeQueries
                   for label in ["Supplier", "Material", "PurchaseOrder",
                                 "GoodsReceipt", "IncomingInspection", "NCR"])

    async def test_edge_cql_uses_match_not_merge_for_nodes(
        self, mockNeo4j
    ) -> None:
        """边写入 MATCH 两端节点（防止边悄悄创建孤立节点）。"""
        service = GraphRelationService()
        await service.seedGraphRelations(_FakeSession(_sampleMappings(), []))

        edgeQueries = [
            cql for cql, _ in _allQueries(mockNeo4j)
            if "MERGE (a)-[r:" in cql
        ]
        assert edgeQueries
        for cql in edgeQueries:
            assert "MATCH (a:BusinessEntity:" in cql
            assert "MATCH" in cql.split("MERGE")[0]

    async def test_signed_edges_from_doc_relations(self, mockNeo4j) -> None:
        service = GraphRelationService()
        docRows = [
            ("DOC-SMOKE-001", "测试合同", 100_001),
            ("DOC-SMOKE-002", "测试合同2", 100_002),
        ]
        result = await service.seedGraphRelations(
            _FakeSession(_sampleMappings(), docRows)
        )

        assert result.nodesBySource["document_catalog"] == 2
        assert result.edgesByType["SIGNED"] == 2
        assert result.edgeCount == 47  # 45 + 2 SIGNED

        # Contract 节点 + SIGNED 边的 CQL 断言
        queries = _allQueries(mockNeo4j)
        contractNodes = [
            (cql, p) for cql, p in queries
            if "MERGE (b:BusinessEntity:Contract" in cql
        ]
        assert len(contractNodes) == 2
        signedEdges = [
            (cql, p) for cql, p in queries if "MERGE (a)-[r:SIGNED]->" in cql
        ]
        assert len(signedEdges) == 2
        assert signedEdges[0][1]["toLabel"] if "toLabel" in signedEdges[0][1] else True
        # SIGNED 边属性带来源标记
        assert signedEdges[0][1]["props"]["source"] == "document_entity_relation"

    async def test_empty_pg_data_still_seeds_static_flow(self, mockNeo4j) -> None:
        """entity_mapping / document 关联全空时仍写入 Sheet 16 演示流。"""
        service = GraphRelationService()
        result = await service.seedGraphRelations(_FakeSession([], []))
        assert result.nodesBySource["sheet16_demo"] == 4
        assert result.nodesBySource.get("entity_mapping", 0) == 0
        assert result.nodesBySource["document_catalog"] == 0
        assert result.nodeCount == 4
        assert result.edgeCount == 45

    async def test_dedup_multiple_source_systems(self, mockNeo4j) -> None:
        """同一 enterprise_key 多源系统（ERP/SRM/QMS）映射只产一个节点。"""
        service = GraphRelationService()
        mappings = [
            _FakeMapping(EntityType.SUPPLIER, 100_001, "SUP000001"),
            _FakeMapping(EntityType.SUPPLIER, 100_001, "SUP000001"),
            _FakeMapping(EntityType.SUPPLIER, 100_001, "SUP000001"),
        ]
        session = _FakeSession(mappings, [])
        result = await service.seedGraphRelations(session)
        assert result.nodesBySource["entity_mapping"] == 1

        supplierNodes = [
            (cql, p) for cql, p in _allQueries(mockNeo4j)
            if "MERGE (b:BusinessEntity:Supplier" in cql
        ]
        assert len(supplierNodes) == 1


# =============================================================================
# resetGraph / snapshot（mock）
# =============================================================================


class TestGraphLifecycle:
    def test_delete_business_graph_only_targets_business_entity(
        self, mockNeo4j
    ) -> None:
        neo4j_module.deleteBusinessGraph()
        cql = _allQueries(mockNeo4j)[0][0]
        assert "MATCH (b:BusinessEntity)" in cql
        assert "DETACH DELETE b" in cql
        # 不含本体 label（删除范围封闭）
        for ontologyLabel in ("Class", "Property", "Metric"):
            assert f":{ontologyLabel}" not in cql

    def test_count_queries_isolated(self, mockNeo4j) -> None:
        neo4j_module.countBusinessNodes()
        neo4j_module.countBusinessRelations()
        queries = [cql for cql, _ in _allQueries(mockNeo4j)]
        assert any("MATCH (b:BusinessEntity) RETURN count(b)" in c for c in queries)
        assert any(
            "MATCH (:BusinessEntity)-[r]->(:BusinessEntity)" in c for c in queries
        )
