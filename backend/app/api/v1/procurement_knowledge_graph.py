"""M2-2b 采购受控邻接：`GET /api/v1/procurement/knowledge/graph/neighbors`。

设计稿 §8.3：`entityType / entityKey / relationTypes[] / maxHops` ⇒
`nodes[] / edges[] / truncated`。

**全部守卫复用 `GraphTraversalService.traverse`**（label 白名单 / 起点存在性
404 / 深度 [1,5] / 图侧关系白名单 Cypher）——本文件零新增图查询；视图变换
（hops → nodes/edges + 白名单交集 + 截断）在
`services/knowledge/graph_neighbors.buildNeighborsView`（纯函数）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.graph_traversal import getGraphTraversalService
from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.knowledge_schemas import ProcurementGraphNeighborsRead
from app.services.graph_traversal_service import GraphTraversalService
from app.services.knowledge.graph_neighbors import buildNeighborsView

router = APIRouter(tags=["procurement-knowledge"])


@router.get(
    "/graph/neighbors",
    response_model=ProcurementGraphNeighborsRead,
)
async def getProcurementGraphNeighbors(
    response: Response,
    entityType: str = Query(min_length=1, max_length=50),
    entityKey: str = Query(min_length=1, max_length=100),
    relationTypes: list[str] | None = Query(default=None),
    maxHops: int = Query(default=2, ge=1, le=5),
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: GraphTraversalService = Depends(getGraphTraversalService),
) -> ProcurementGraphNeighborsRead:
    """受控邻接：统一实体 → 白名单内的多跳邻居（nodes/edges/truncated 自证）。"""
    response.headers["Cache-Control"] = "no-store"
    traversal = await service.traverse(entityType, entityKey, maxHops)
    return buildNeighborsView(
        traversal.hops,
        startType=entityType,
        startKey=entityKey,
        maxHops=maxHops,
        requestedRelationTypes=relationTypes,
    )
