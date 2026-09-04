"""Phase 6.2 业务关系图服务（feat-semantic-relations）。

职责：把 Neo4j 从「本体图」（Class/Property/Metric）扩展为「业务关系图」
（BusinessEntity 子图），数据来源三路：

1. ``entity_mapping``（Phase 3 跨系统主数据映射）-> Supplier / Material /
   PurchaseOrder / GoodsReceipt / IncomingInspection 实体节点；
2. ``document_catalog`` + ``document_entity_relation``（Phase 5.1 文档目录）
   -> Contract 节点 + Supplier-SIGNED->Contract 边；
3. Sheet 16 采购业务流转演示数据（静态，同 seed_entity_mapping 的 Sheet 04/05
   语义）-> 补充 GR/IQC/NCR 流转实例 + 全部实体级关系边。

关系语义（Sheet 16 采购域流转）::

    (:Supplier)-[:SUPPLIES]->(:Material)
    (:PurchaseOrder)-[:CONTAINS]->(:Material)
    (:PurchaseOrder)-[:GENERATES]->(:GoodsReceipt)
    (:GoodsReceipt)-[:INSPECTED_BY]->(:IncomingInspection)
    (:IncomingInspection)-[:GENERATED]->(:NCR)
    (:Supplier)-[:SIGNED]->(:Contract)

设计约束：
- 幂等：节点 MERGE on key、边 MERGE on (两端 + 类型)，重复 seed 不产生重复数据；
- 隔离：业务节点统一携带 ``BusinessEntity`` 主 label，本体同步/删除路径
  （_ALLOWED_LABELS = Class/Property/Metric）永不触碰业务子图；
- 白名单：label 与关系类型均经 ``BUSINESS_ENTITY_LABELS`` /
  ``BUSINESS_RELATION_TYPES`` 校验后拼 CQL（防注入）；
- 边写入用 MATCH 两端（非 MERGE），防止边悄悄创建孤立节点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.domain.models import DocumentCatalog, DocumentEntityRelation, EntityMapping
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure.neo4j_client import (
    BUSINESS_ENTITY_LABELS,
    BUSINESS_RELATION_TYPES,
)

logger = logging.getLogger(__name__)

# EntityType -> Neo4j 业务节点 label（Contract 无 EntityType，由文档目录提供）
ENTITY_TYPE_LABELS: dict[str, str] = {
    "SUPPLIER": "Supplier",
    "MATERIAL": "Material",
    "PO": "PurchaseOrder",
    "GR": "GoodsReceipt",
    "IQC": "IncomingInspection",
    "NCR": "NCR",
}

# Sheet 16 演示流转补充实例（entity_mapping 未覆盖的 GR002/GR003/IQC002/NCR001）：
# 键位与 seed_entity_mapping 的 400_001/500_001 同段位，保证子图键空间一致。
_SHEET16_EXTRA_ENTITIES: tuple[tuple[str, str, str], ...] = (
    ("GoodsReceipt", "400002", "GR202608002"),
    ("GoodsReceipt", "400003", "GR202608003"),
    ("IncomingInspection", "500002", "IQC202608002"),
    ("NCR", "600001", "NCR202608001"),
)


@dataclass(frozen=True)
class GraphSeedResult:
    """seed 执行结果（不可变）。"""

    nodeCount: int
    edgeCount: int
    nodesBySource: dict[str, int] = field(default_factory=dict)
    edgesByType: dict[str, int] = field(default_factory=dict)


class GraphRelationService:
    """业务关系图构建器：PG 实体数据 -> Neo4j BusinessEntity 子图。"""

    async def seedGraphRelations(self, session) -> GraphSeedResult:
        """全量回填业务实体节点 + 关系边（幂等）。返回节点/边计数。"""
        nodesBySource: dict[str, int] = {}
        edgesByType: dict[str, int] = {}

        # ---- 1. entity_mapping -> 业务实体节点 ------------------------------
        mappings = (await session.execute(select(EntityMapping))).scalars().all()
        for entityType, label in ENTITY_TYPE_LABELS.items():
            rows = self._dedupeByType(mappings, entityType)
            for m in rows:
                neo4j.upsertBusinessEntityNode(
                    label=label,
                    key=str(m.enterprise_key),
                    code=m.enterprise_code,
                    name=m.enterprise_code,
                    source="entity_mapping",
                )
            if rows:
                nodesBySource["entity_mapping"] = (
                    nodesBySource.get("entity_mapping", 0) + len(rows)
                )

        # ---- 2. Sheet 16 补充流转实例节点 -----------------------------------
        for label, key, code in _SHEET16_EXTRA_ENTITIES:
            neo4j.upsertBusinessEntityNode(
                label=label, key=key, code=code, name=code, source="sheet16_demo"
            )
        nodesBySource["sheet16_demo"] = len(_SHEET16_EXTRA_ENTITIES)

        # ---- 3. 文档目录 -> Contract 节点 + SIGNED 边 ------------------------
        signedPairs = await self._signedContractPairs(session)
        for docId, docName, supplierKey in signedPairs:
            neo4j.upsertBusinessEntityNode(
                label="Contract",
                key=docId,
                code=docId,
                name=docName,
                source="document_catalog",
            )
            neo4j.linkBusinessRelation(
                relType="SIGNED",
                fromLabel="Supplier",
                fromKey=supplierKey,
                toLabel="Contract",
                toKey=docId,
                properties={"source": "document_entity_relation"},
            )
        nodesBySource["document_catalog"] = len(signedPairs)
        if signedPairs:
            edgesByType["SIGNED"] = len(signedPairs)

        # ---- 4. Sheet 16 流转边（确定性派生自 seed 键位） --------------------
        for relType, pairs in self._sheet16Edges():
            for fromKey, toKey, fromLabel, toLabel in pairs:
                neo4j.linkBusinessRelation(
                    relType=relType,
                    fromLabel=fromLabel,
                    fromKey=fromKey,
                    toLabel=toLabel,
                    toKey=toKey,
                    properties={"source": "sheet16_demo"},
                )
            edgesByType[relType] = edgesByType.get(relType, 0) + len(pairs)

        result = GraphSeedResult(
            nodeCount=sum(nodesBySource.values()),
            edgeCount=sum(edgesByType.values()),
            nodesBySource=nodesBySource,
            edgesByType=edgesByType,
        )
        logger.info(
            "[seed_graph_relations] nodes=%s edges=%s byType=%s",
            result.nodeCount,
            result.edgeCount,
            result.edgesByType,
        )
        return result

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _dedupeByType(
        self, mappings: list[EntityMapping], entityType: str
    ) -> list[EntityMapping]:
        """按 (entity_type, enterprise_key) 去重（同一实体多源系统映射只产一个节点）。"""
        seen: dict[int, EntityMapping] = {}
        for m in mappings:
            if m.entity_type == entityType and m.enterprise_key not in seen:
                seen[m.enterprise_key] = m
        return list(seen.values())

    async def _signedContractPairs(self, session) -> list[tuple[str, str, str]]:
        """查询 Supplier-SIGNED->Contract 三元组（document_entity_relation CONTRACT 关联）。

        返回 [(document_id, document_name, supplier_key_str)]；文档缺失或关联为
        空时返回空列表（不阻断 seed）。
        """
        rows = (
            await session.execute(
                select(
                    DocumentEntityRelation.document_id,
                    DocumentCatalog.document_name,
                    DocumentEntityRelation.entity_key,
                )
                .join(
                    DocumentCatalog,
                    DocumentCatalog.document_id
                    == DocumentEntityRelation.document_id,
                )
                .where(
                    DocumentEntityRelation.entity_type == "SUPPLIER"
                )
            )
        ).all()
        return [
            (docId, docName or docId, str(entityKey))
            for docId, docName, entityKey in rows
        ]

    def _sheet16Edges(
        self,
    ) -> list[tuple[str, list[tuple[str, str, str, str]]]]:
        """Sheet 16 流转边派生（确定性，键位与 seed_entity_mapping 对齐）。

        - SUPPLIES：供应商 i 供应物料 {(3i-2, 3i-1, 3i) mod 10}，10 供应商 × 3 = 30 条；
        - CONTAINS：PO202608{i} 含物料 3i-2..3i（i=1..3）= 9 条；
        - GENERATES：PO001-003 -> GR001-003 = 3 条；
        - INSPECTED_BY：GR001/GR002 -> IQC001/IQC002 = 2 条；
        - GENERATED：IQC001 -> NCR001 = 1 条。
        共 45 条（加 SIGNED 边后 ≥ Phase 6 验收要求的 30 条）。
        """
        supplierCount = 10
        materialCount = 10

        supplies = [
            (str(100_000 + i), str(200_000 + materialIndex), "Supplier", "Material")
            for i in range(1, supplierCount + 1)
            for materialIndex in self._suppliedMaterialIndexes(i, materialCount)
        ]
        contains = [
            (str(300_000 + i), str(200_000 + m), "PurchaseOrder", "Material")
            for i in (1, 2, 3)
            for m in (3 * i - 2, 3 * i - 1, 3 * i)
        ]
        poGenerates = [
            (str(300_000 + i), str(400_000 + i), "PurchaseOrder", "GoodsReceipt")
            for i in (1, 2, 3)
        ]
        inspectedBy = [
            (str(400_000 + i), str(500_000 + i), "GoodsReceipt", "IncomingInspection")
            for i in (1, 2)
        ]
        generated = [
            (str(500_001), str(600_001), "IncomingInspection", "NCR")
        ]
        return [
            ("SUPPLIES", supplies),
            ("CONTAINS", contains),
            ("GENERATES", poGenerates),
            ("INSPECTED_BY", inspectedBy),
            ("GENERATED", generated),
        ]

    def _suppliedMaterialIndexes(self, supplierIndex: int, materialCount: int) -> list[int]:
        """供应商 i 供应的物料下标（1-based，环形取 3 个，确定性）。

        i=1 -> {1,2,3}；i=2 -> {4,5,6}；i=4 -> {10,1,2}……每个供应商 3 个物料，
        全体合计 30 条不重复 (supplier, material) 对。
        """
        base = (3 * (supplierIndex - 1)) % materialCount
        return [
            (base + offset) % materialCount + 1 for offset in (0, 1, 2)
        ]

    # ------------------------------------------------------------------
    # 校验 / 查询（供 seed 脚本与测试使用）
    # ------------------------------------------------------------------

    def validateSchema(self) -> list[str]:
        """校验本服务用到的 label / 关系类型均在 Neo4j 客户端白名单内。

        返回违规项列表（空 = 通过）；防止两边常量漂移。
        """
        violations = [
            label
            for label in ENTITY_TYPE_LABELS.values()
            if label not in BUSINESS_ENTITY_LABELS
        ]
        if "Contract" not in BUSINESS_ENTITY_LABELS:
            violations.append("Contract")
        for label, _, _ in _SHEET16_EXTRA_ENTITIES:
            if label not in BUSINESS_ENTITY_LABELS:
                violations.append(label)
        for relType in ("SUPPLIES", "CONTAINS", "GENERATES", "INSPECTED_BY", "GENERATED", "SIGNED"):
            if relType not in BUSINESS_RELATION_TYPES:
                violations.append(relType)
        return violations

    def getGraphSnapshot(self) -> dict:
        """业务关系图快照（节点 + 边），供验证与前端渲染。"""
        return neo4j.getBusinessGraphSnapshot()

    def resetGraph(self) -> int:
        """清空业务子图（保留本体图）。返回删除节点数。"""
        return neo4j.deleteBusinessGraph()
