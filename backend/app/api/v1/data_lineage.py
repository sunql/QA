"""数据血缘 API 路由（Phase 2.1）。

挂在 /api/v1/lineage/edges：
  GET    /api/v1/lineage/edges          列表（按 sourceLayer / targetLayer / activeOnly 过滤）
  GET    /api/v1/lineage/edges/{id}     详情
  POST   /api/v1/lineage/edges          创建（201）
  PUT    /api/v1/lineage/edges/{id}     更新
  DELETE /api/v1/lineage/edges/{id}     软删除（204；is_active=false）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getDb
from app.domain.enums import LineageLayer
from app.domain.schemas import LineageEdgeCreate, LineageEdgeRead, LineageEdgeUpdate
from app.services.data_lineage_service import DataLineageService, lineageToRead

router = APIRouter(dependencies=[])


def getDataLineageService() -> DataLineageService:
    """service 无状态依赖，直接返回新实例；保留工厂风格便于后续注入。"""
    return DataLineageService()


@router.get("", response_model=list[LineageEdgeRead])
async def listLineageEdges(
    sourceLayer: LineageLayer | None = Query(default=None, alias="sourceLayer"),
    targetLayer: LineageLayer | None = Query(default=None, alias="targetLayer"),
    activeOnly: bool | None = Query(default=None, alias="activeOnly"),
    session: AsyncSession = Depends(getDb),
    service: DataLineageService = Depends(getDataLineageService),
) -> list[LineageEdgeRead]:
    edges = await service.listEdges(
        session,
        sourceLayer=sourceLayer,
        targetLayer=targetLayer,
        activeOnly=activeOnly,
    )
    return [lineageToRead(e) for e in edges]


@router.get("/{edgeId}", response_model=LineageEdgeRead)
async def getLineageEdge(
    edgeId: int,
    session: AsyncSession = Depends(getDb),
    service: DataLineageService = Depends(getDataLineageService),
) -> LineageEdgeRead:
    edge = await service.getEdge(session, edgeId)
    return lineageToRead(edge)


@router.post("", response_model=LineageEdgeRead, status_code=status.HTTP_201_CREATED)
async def createLineageEdge(
    payload: LineageEdgeCreate,
    session: AsyncSession = Depends(getDb),
    service: DataLineageService = Depends(getDataLineageService),
) -> LineageEdgeRead:
    edge = await service.createEdge(session, payload)
    return lineageToRead(edge)


@router.put("/{edgeId}", response_model=LineageEdgeRead)
async def updateLineageEdge(
    edgeId: int,
    payload: LineageEdgeUpdate,
    session: AsyncSession = Depends(getDb),
    service: DataLineageService = Depends(getDataLineageService),
) -> LineageEdgeRead:
    edge = await service.updateEdge(session, edgeId, payload)
    return lineageToRead(edge)


@router.delete("/{edgeId}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteLineageEdge(
    edgeId: int,
    session: AsyncSession = Depends(getDb),
    service: DataLineageService = Depends(getDataLineageService),
) -> None:
    await service.disableEdge(session, edgeId)