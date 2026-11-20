"""M2-2b 受控邻接视图（设计稿 §8.3 `GET /procurement/knowledge/graph/neighbors`）。

**复用既有守卫，不重复实现**：label 白名单 / 起点存在性（404）/ 深度 [1,5] /
图侧关系白名单 Cypher 全部来自 `GraphTraversalService.traverse`；本模块只做
**视图变换**（hops → nodes/edges）+ 两层受控：

1. **关系白名单交集**：请求 relationTypes ∩ `BUSINESS_RELATION_TYPES`
   （neo4j_client 单点事实）；白名单外类型不可遍历（静默丢弃 +
   `effectiveRelationTypes` 自证——调用方能看出哪些请求类型被丢弃）；
2. **边数上限**：`MAX_NEIGHBOR_EDGES` 截断 + `truncated` 置位（真实规模
   92 万 CONTAINS 行，深跳子图不能原样塞进响应）。
"""

from __future__ import annotations

from typing import Any

from app.domain.knowledge_schemas import (
    ProcurementGraphEdgeRead,
    ProcurementGraphNeighborsRead,
    ProcurementGraphNodeRead,
)
from app.infrastructure.neo4j_client import BUSINESS_RELATION_TYPES

#: 邻接响应的边数上限（真实规模：92 万 CONTAINS 行 / 35 万 SUPPLIES 对）。
MAX_NEIGHBOR_EDGES = 200


def buildNeighborsView(
    hops: list[Any],
    *,
    startType: str,
    startKey: str,
    maxHops: int,
    requestedRelationTypes: list[str] | None,
    maxEdges: int = MAX_NEIGHBOR_EDGES,
) -> ProcurementGraphNeighborsRead:
    """遍历 hops → 受控邻接视图（纯函数，返回新对象，不改动入参）。"""
    requested = [t for t in (requestedRelationTypes or []) if t]
    effective = (
        tuple(sorted(set(requested) & BUSINESS_RELATION_TYPES))
        if requested
        else tuple(sorted(BUSINESS_RELATION_TYPES))
    )

    edges: list[ProcurementGraphEdgeRead] = []
    nodesByKey: dict[str, ProcurementGraphNodeRead] = {}
    truncated = False
    for hop in hops:
        if hop.rel_type not in effective:
            continue
        if len(edges) >= maxEdges:
            truncated = True
            break
        edge = ProcurementGraphEdgeRead(
            depth=hop.depth,
            from_type=hop.from_type,
            from_key=hop.from_key,
            from_code=hop.from_code,
            from_name=hop.from_name,
            rel_type=hop.rel_type,
            to_type=hop.to_type,
            to_key=hop.to_key,
            to_code=hop.to_code,
            to_name=hop.to_name,
        )
        edges.append(edge)
        nodesByKey.setdefault(
            edge.from_key,
            ProcurementGraphNodeRead(
                entity_type=hop.from_type,
                key=hop.from_key,
                code=hop.from_code,
                name=hop.from_name,
            ),
        )
        nodesByKey.setdefault(
            edge.to_key,
            ProcurementGraphNodeRead(
                entity_type=hop.to_type,
                key=hop.to_key,
                code=hop.to_code,
                name=hop.to_name,
            ),
        )
    else:
        # 无 break ⇒ 全部 hops 处理完；truncated 只在 break 时置位
        pass

    return ProcurementGraphNeighborsRead(
        entity_type=startType,
        entity_key=startKey,
        max_hops=maxHops,
        effective_relation_types=effective,
        nodes=sorted(
            nodesByKey.values(),
            key=lambda n: (n.entity_type, n.key),
        ),
        edges=edges,
        truncated=truncated,
    )
