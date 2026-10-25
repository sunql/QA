"""业务图 THBI 回填集成测试（真实 Neo4j，Oracle 侧用构造行注入）。

覆盖 applyGraphPlan 端到端：
- 节点 + 三类边（PLACES / SUPPLIES / CONTAINS）真实入图；
- 幂等：重复 apply 图快照不变；
- 遍历语义：Supplier 起点 1 跳见 PO/物料、2 跳见 PO-CONTAINS->Material 链；
- 白名单接线：validateSchema 通过（PLACES 已纳入）。

Neo4j 不可达时整文件跳过；每用例前后清空 BusinessEntity 子图（同
test_graph_relations_integration.py 约定，本体图不受影响）。
"""

from __future__ import annotations

import pytest

from app.infrastructure import neo4j_client as neo4j
from app.services.graph_relation_service import GraphRelationService
from app.services.graph_traversal_service import GraphTraversalService
from scripts.sync_business_graph_from_thbi import applyGraphPlan, buildGraphPlan

_neo4jAvailable = neo4j.isNeo4jAvailable()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _neo4jAvailable, reason="Neo4j 不可达，跳过业务图回填集成测试"),
]


@pytest.fixture(autouse=True)
def cleanBusinessGraph():
    neo4j.deleteBusinessGraph()
    yield
    neo4j.deleteBusinessGraph()


def _samplePlan():
    headers = [
        {"po_no": "PO1", "supplier_code": "S1", "supplier_name": "甲供应商"},
        {"po_no": "PO2", "supplier_code": "S1", "supplier_name": "甲供应商"},
        {"po_no": "PO3", "supplier_code": "S2", "supplier_name": "乙供应商"},
    ]
    lines = [
        {"po_no": "PO1", "material_code": "M1", "supplier_code": "S1"},
        {"po_no": "PO1", "material_code": "M2", "supplier_code": "S1"},
        {"po_no": "PO2", "material_code": "M1", "supplier_code": "S1"},
        {"po_no": "PO3", "material_code": "M3", "supplier_code": "S2"},
    ]
    return buildGraphPlan(headers, lines)


class TestApplyGraphPlan:
    async def test_writes_nodes_and_all_three_edge_types(self) -> None:
        plan = _samplePlan()
        result = await applyGraphPlan(plan)

        assert result["nodes"] == 8  # 2 供应商 + 3 PO + 3 物料
        assert result["edges"] == 10  # PLACES 3 + SUPPLIES 3（去重）+ CONTAINS 4

        node = neo4j.getBusinessNode("Supplier", "S1")
        assert node is not None
        assert node["name"] == "甲供应商"
        assert node["source"] == "thbi_dwd"

        snapshot = GraphRelationService().getGraphSnapshot()
        edgeTypes = {e["relType"] for e in snapshot["edges"]}
        assert {"PLACES", "SUPPLIES", "CONTAINS"} <= edgeTypes

    async def test_reapply_is_idempotent(self) -> None:
        plan = _samplePlan()
        await applyGraphPlan(plan)
        snapshotBefore = GraphRelationService().getGraphSnapshot()

        result = await applyGraphPlan(plan)
        snapshotAfter = GraphRelationService().getGraphSnapshot()

        assert result["nodes"] == 8  # MERGE 语义：重放仍返回触及行数
        assert result["edges"] == 10
        assert len(snapshotAfter["nodes"]) == len(snapshotBefore["nodes"])
        assert len(snapshotAfter["edges"]) == len(snapshotBefore["edges"])

    async def test_traversal_from_supplier_end_to_end(self) -> None:
        plan = _samplePlan()
        await applyGraphPlan(plan)

        service = GraphTraversalService()
        result = await service.traverse("Supplier", "S1", 2)

        depth1 = {(h.to_type, h.to_key) for h in result.hops if h.depth == 1}
        # 1 跳：下单的 PO 与供应的物料（ traversal 方向不限）
        assert ("PurchaseOrder", "PO1") in depth1
        assert ("PurchaseOrder", "PO2") in depth1
        assert ("ItemMaster", "M1") in depth1
        assert ("ItemMaster", "M2") in depth1

        depth2 = {(h.from_type, h.rel_type, h.to_type, h.to_key) for h in result.hops if h.depth == 2}
        # 2 跳：S1 -SUPPLIES-> M1 <-CONTAINS- PO2（经物料间接可达另一张单）
        assert ("ItemMaster", "CONTAINS", "PurchaseOrder", "PO2") in depth2

        assert "PurchaseOrder" in result.reachable_types
        assert "ItemMaster" in result.reachable_types

    async def test_validate_schema_accepts_places(self) -> None:
        assert GraphRelationService().validateSchema() == []
