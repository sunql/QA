"""Phase 6.2 业务关系图集成测试（真实 PostgreSQL + 真实 Neo4j）。

覆盖：
- seedGraphRelations 端到端：entity_mapping（真实 seed）-> 节点 + Sheet 16 流转边；
- 幂等：重复 seed 节点/边数量不变；
- 图隔离：业务图写入/清空不影响本体节点（Class/Property/Metric）；
- 多跳链路：Supplier-SIGNED->Contract、Supplier-SUPPLIES->Material<-CONTAINS-PurchaseOrder；
- SIGNED 边来自 document_entity_relation（真实 PG 关联查询）。

Neo4j 不可达时整文件跳过（pytest.skip，模块级探测）：
「不可让 Neo4j/Milvus 失败阻断 PG」（ai-roadmap §6 关键约束）。
每用例前后清空 BusinessEntity 子图（deleteBusinessGraph），不污染真实图库。
"""

from __future__ import annotations

import pytest

from app.domain.enums import DocEntityRelationType, EntityType
from app.domain.models import DocumentEntityRelation, EntityMapping
from app.infrastructure import neo4j_client as neo4j
from app.services.graph_relation_service import GraphRelationService
from scripts.seed_entity_mapping import seedEntityMappings

_neo4jAvailable = neo4j.isNeo4jAvailable()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _neo4jAvailable, reason="Neo4j 不可达，跳过业务图集成测试"),
]


@pytest.fixture(autouse=True)
async def cleanBusinessGraph(dbSession):
    """每用例前后清空业务子图（本体图不受影响），并保证 entity_mapping 有种子。"""
    neo4j.deleteBusinessGraph()
    await seedEntityMappings(dbSession)
    yield
    neo4j.deleteBusinessGraph()


class TestSeedGraphRelationsEndToEnd:
    async def test_seed_creates_nodes_and_edges(self, dbSession) -> None:
        service = GraphRelationService()
        result = await service.seedGraphRelations(dbSession)

        # entity_mapping 45 条映射去重后 25 实体（10 SUP + 10 MAT + 3 PO + 1 GR + 1 IQC）
        assert result.nodesBySource["entity_mapping"] == 25
        assert result.nodesBySource["sheet16_demo"] == 4
        assert result.nodeCount >= 29

        # Phase 6 验收线：业务关系 >= 30 条
        assert result.edgeCount >= 30
        assert result.edgesByType["SUPPLIES"] == 30
        assert result.edgesByType["CONTAINS"] == 9
        assert result.edgesByType["GENERATES"] == 3
        assert result.edgesByType["INSPECTED_BY"] == 2
        assert result.edgesByType["GENERATED"] == 1

        # 图内实际计数一致
        assert neo4j.countBusinessNodes() == result.nodeCount
        assert neo4j.countBusinessRelations() == result.edgeCount

    async def test_seed_idempotent(self, dbSession) -> None:
        service = GraphRelationService()
        first = await service.seedGraphRelations(dbSession)
        second = await service.seedGraphRelations(dbSession)

        assert neo4j.countBusinessNodes() == first.nodeCount
        assert neo4j.countBusinessRelations() == first.edgeCount
        # 第二次结果计数一致（MERGE 幂等，不产生重复）
        assert second.nodeCount == first.nodeCount
        assert second.edgeCount == first.edgeCount

    async def test_business_graph_isolated_from_ontology(self, dbSession) -> None:
        """业务图写入 + 清空全程本体节点数不变（图隔离验收）。"""
        before = len(neo4j.getAllClassNodes())
        service = GraphRelationService()
        await service.seedGraphRelations(dbSession)
        assert len(neo4j.getAllClassNodes()) == before

        deleted = neo4j.deleteBusinessGraph()
        assert deleted > 0
        assert len(neo4j.getAllClassNodes()) == before
        assert neo4j.countBusinessNodes() == 0
        assert neo4j.countBusinessRelations() == 0

    async def test_multi_hop_supply_chain(self, dbSession) -> None:
        """Supplier -> Material <- PurchaseOrder 三跳链路可查（6.3 遍历 API 前置验证）。"""
        service = GraphRelationService()
        await service.seedGraphRelations(dbSession)

        snapshot = service.getGraphSnapshot()
        # 供应商 100001 供应 3 个物料
        suppliesFrom100001 = [
            e for e in snapshot["edges"]
            if e["relType"] == "SUPPLIES" and e["fromKey"] == "100001"
        ]
        assert len(suppliesFrom100001) == 3

        # 每个被供应物料均被某 PO CONTAINS（闭环供应链）
        materialKeys = {e["toKey"] for e in suppliesFrom100001}
        containsKeys = {e["toKey"] for e in snapshot["edges"] if e["relType"] == "CONTAINS"}
        assert materialKeys <= containsKeys

        # PO -> GR -> IQC 完整流转链
        poToGr = {
            (e["fromKey"], e["toKey"]) for e in snapshot["edges"]
            if e["relType"] == "GENERATES" and e["fromType"] == "PurchaseOrder"
        }
        assert ("300001", "400001") in poToGr
        grToIqc = {
            (e["fromKey"], e["toKey"]) for e in snapshot["edges"]
            if e["relType"] == "INSPECTED_BY"
        }
        assert ("400001", "500001") in grToIqc

    async def test_signed_edges_require_pg_relation(self, dbSession) -> None:
        """SIGNED 边仅来自 document_entity_relation（无关联 -> 0 条）。"""
        # 清掉种子可能写入的 doc 关联（cleanBusinessGraph 只重放 entity_mapping）
        from sqlalchemy import delete

        await dbSession.execute(delete(DocumentEntityRelation))
        await dbSession.commit()

        service = GraphRelationService()
        result = await service.seedGraphRelations(dbSession)
        assert result.edgesByType.get("SIGNED", 0) == 0
        assert result.nodesBySource["document_catalog"] == 0

        # 写入一条 CONTRACT 关联（+ 对应文档目录行，join 才有结果）后重放
        # -> 1 条 SIGNED 边 + 1 个 Contract 节点
        from app.domain.enums import DocumentType
        from app.domain.models import DocumentCatalog

        dbSession.add(
            DocumentCatalog(
                document_id="DOC-SMOKE-001",
                document_name="测试合同",
                document_type=DocumentType.CONTRACT,
            )
        )
        dbSession.add(
            DocumentEntityRelation(
                document_id="DOC-SMOKE-001",
                entity_type=EntityType.SUPPLIER.value,
                entity_key=100_001,
                relation_type=DocEntityRelationType.CONTRACT,
            )
        )
        await dbSession.commit()

        result = await service.seedGraphRelations(dbSession)
        assert result.edgesByType["SIGNED"] == 1
        assert result.nodesBySource["document_catalog"] == 1

        snapshot = service.getGraphSnapshot()
        contractNodes = [n for n in snapshot["nodes"] if n["entityType"] == "Contract"]
        assert len(contractNodes) == 1
        assert contractNodes[0]["key"] == "DOC-SMOKE-001"
        assert contractNodes[0]["name"] == "测试合同"

    async def test_node_dedup_across_source_systems(self, dbSession) -> None:
        """entity_mapping 45 条（含同实体多源）-> 去重后 25 个实体节点。"""
        service = GraphRelationService()
        mappings = (await dbSession.execute(
            __import__("sqlalchemy").select(EntityMapping)
        )).scalars().all()
        # seed_entity_mapping 45 条：25 SUP + 15 MAT + 3 PO + 1 GR + 1 IQC
        assert len(mappings) == 45

        await service.seedGraphRelations(dbSession)
        supplierNodes = [
            n for n in service.getGraphSnapshot()["nodes"]
            if n["entityType"] == "Supplier"
        ]
        assert len(supplierNodes) == 10  # 25 条供应商映射去重为 10 个节点

    async def test_snapshot_schema_contract(self, dbSession) -> None:
        """快照节点/边字段齐全（供 6.3 遍历 API 与前端渲染的稳定契约）。"""
        service = GraphRelationService()
        await service.seedGraphRelations(dbSession)
        snapshot = service.getGraphSnapshot()

        assert {"nodes", "edges"} <= set(snapshot)
        for node in snapshot["nodes"]:
            assert {"key", "code", "name", "entityType", "source", "labels"} <= set(node)
            assert "BusinessEntity" in node["labels"]
        for edge in snapshot["edges"]:
            assert {"fromKey", "fromType", "relType", "toKey", "toType"} <= set(edge)
            assert edge["relType"] in neo4j.BUSINESS_RELATION_TYPES
