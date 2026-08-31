"""Phase 6.2 业务关系图种子脚本（幂等）。

一次性回填 Neo4j BusinessEntity 子图：
1. 确保 entity_mapping 已有种子（调用 seed_entity_mapping.seedEntityMappings，
   幂等，已有数据静默跳过）；
2. 种子 document_entity_relation（4 份既有合同 -> 前 2 家供应商，CONTRACT 关联，
   INSERT ON CONFLICT DO NOTHING）；
3. 调用 GraphRelationService.seedGraphRelations() 写入业务实体节点 + 关系边。

来源语义（Sheet 16 采购域业务流转）::

    (:Supplier)-[:SUPPLIES]->(:Material)
    (:PurchaseOrder)-[:CONTAINS]->(:Material)
    (:PurchaseOrder)-[:GENERATES]->(:GoodsReceipt)
    (:GoodsReceipt)-[:INSPECTED_BY]->(:IncomingInspection)
    (:IncomingInspection)-[:GENERATED]->(:NCR)
    (:Supplier)-[:SIGNED]->(:Contract)

幂等性：节点 MERGE on key、边 MERGE on (两端 + 类型)，重复运行不产生重复数据。
运行：`cd backend && .venv/bin/python -m scripts.seed_graph_relations`
（或 `python scripts/seed_graph_relations.py`，需 PG 5433 + Neo4j 7687 可达）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.domain.enums import DocEntityRelationType, EntityType  # noqa: E402
from app.domain.models import DocumentEntityRelation, EntityMapping  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.graph_relation_service import GraphRelationService  # noqa: E402
from scripts.seed_entity_mapping import seedEntityMappings  # noqa: E402

# 合同 -> 供应商 关联种子（Sheet 16 语义：主供应商签框架合同）。
# 键位与 seed_entity_mapping 的供应商 100001/100002 对齐。
_DOC_RELATION_SEED: tuple[tuple[str, int], ...] = (
    ("DOC-SMOKE-001", 100_001),
    ("DOC-SMOKE-002", 100_002),
)


async def _seedDocumentRelations(session) -> int:
    """幂等写入合同-供应商关联（唯一键冲突静默跳过）。返回本次新增条数。"""
    inserted = 0
    for docId, supplierKey in _DOC_RELATION_SEED:
        result = await session.execute(
            pg_insert(DocumentEntityRelation)
            .values(
                document_id=docId,
                entity_type=EntityType.SUPPLIER.value,
                entity_key=supplierKey,
                relation_type=DocEntityRelationType.CONTRACT,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "document_id",
                    "entity_type",
                    "entity_key",
                    "relation_type",
                ]
            )
        )
        inserted += result.rowcount or 0
    await session.commit()
    return inserted


async def main() -> None:
    factory = getSessionFactory()
    service = GraphRelationService()

    violations = service.validateSchema()
    if violations:
        print(f"[seed_graph_relations] schema 校验失败（白名单漂移）: {violations}")
        sys.exit(1)

    async with factory() as session:
        # 1. entity_mapping 种子（幂等；已有数据时本次新增 0 条）
        mappingInserted = await seedEntityMappings(session)
        mappingTotal = (
            await session.execute(select(func.count()).select_from(EntityMapping))
        ).scalar()
        print(
            f"[seed_graph_relations] entity_mapping 本次新增 {mappingInserted} 条，"
            f"库内共 {mappingTotal} 条"
        )

        # 2. document_entity_relation 种子（幂等）
        relInserted = await _seedDocumentRelations(session)
        print(f"[seed_graph_relations] document_entity_relation 本次新增 {relInserted} 条")

        # 3. 业务关系图回填（幂等 MERGE）
        result = await service.seedGraphRelations(session)

    nodeTotal = service.getGraphSnapshot()
    edgeCount = len(nodeTotal["edges"])
    nodeCount = len(nodeTotal["nodes"])
    print(
        f"[seed_graph_relations] 业务图回填完成："
        f"节点 {nodeCount}（来源 {result.nodesBySource}），"
        f"边 {edgeCount}（类型 {result.edgesByType}）"
    )
    if edgeCount < 30:
        print("[seed_graph_relations] 警告：业务关系边不足 30 条（Phase 6 验收线）")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
