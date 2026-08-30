"""KPI Catalog REST API（Phase 4.1 + Phase 4.5 governance hardening）。

与 data_quality / entity_mapping 同模式：扁平路径，挂在 /api/v1/kpi-catalog。
KPI 不进 Neo4j / Milvus（治理层）。

Phase 4.5：PUT / DELETE 走 AclService（owner-based）；PermissionDenied → 403。
ACL 已在 service 层抛 PermissionDeniedError；不再路由层重复检查。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    KpiCatalogCreate,
    KpiCatalogHistoryRead,
    KpiCatalogRead,
    KpiCatalogUpdate,
)
from app.services.history_service import HistoryService
from app.services.kpi_catalog_service import KpiCatalogService

router = APIRouter(dependencies=[])
_service = KpiCatalogService()
_history_service = HistoryService()


@router.get("", response_model=list[KpiCatalogRead], status_code=status.HTTP_200_OK)
async def listKpis(
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> list[KpiCatalogRead]:
    """列出所有 KPI（按 kpi_code 升序）。"""
    return [KpiCatalogRead.model_validate(e) for e in await _service.listKpis(db)]


@router.post("", response_model=KpiCatalogRead, status_code=status.HTTP_201_CREATED)
async def createKpi(
    dto: KpiCatalogCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KpiCatalogRead:
    """创建 KPI。重复 kpi_code 返回 409。

    CREATE 不做 ACL（任何部门都能创建新 KPI）；审计 + 历史快照由 service 层写入。
    """
    return KpiCatalogRead.model_validate(await _service.createKpi(db, dto, user))


@router.get("/{id}", response_model=KpiCatalogRead, status_code=status.HTTP_200_OK)
async def getKpi(
    id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KpiCatalogRead:
    """按 ID 查询 KPI。"""
    return KpiCatalogRead.model_validate(await _service.getKpi(db, id))


@router.put("/{id}", response_model=KpiCatalogRead, status_code=status.HTTP_200_OK)
async def updateKpi(
    id: int,
    dto: KpiCatalogUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KpiCatalogRead:
    """更新 KPI。owner 不匹配 → 403；revision_count 始终 +1。"""
    return KpiCatalogRead.model_validate(await _service.updateKpi(db, id, dto, user))


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteKpi(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除 KPI。owner 不匹配 → 403；审计 before 写入。"""
    await _service.deleteKpi(db, id, user)


# ---------------------------------------------------------------------------
# Phase 4.5: kpi_catalog_history 回放 API
# ---------------------------------------------------------------------------


@router.get(
    "/{id}/history",
    response_model=list[KpiCatalogHistoryRead],
    status_code=status.HTTP_200_OK,
    summary="KPI 历史快照回放",
)
async def listKpiHistory(
    id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[KpiCatalogHistoryRead]:
    """按 KPI ID 查询其全部历史快照（按 revision 倒序）。"""
    await _service.getKpi(db, id)  # 校验 KPI 存在
    rows = await _history_service.listKpiHistory(db, kpi_id=id, limit=limit, offset=offset)
    return [KpiCatalogHistoryRead.model_validate(r) for r in rows]


@router.get(
    "/{id}/history/{revision}",
    response_model=KpiCatalogHistoryRead,
    status_code=status.HTTP_200_OK,
    summary="KPI 指定 revision 快照",
)
async def getKpiHistoryRevision(
    id: int,
    revision: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KpiCatalogHistoryRead:
    """查指定 KPI 的指定 revision 快照。"""
    await _service.getKpi(db, id)  # 校验 KPI 存在
    row = await _history_service.getKpiHistoryRevision(db, kpi_id=id, revision=revision)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"KPI {id} revision {revision} 不存在",
        )
    return KpiCatalogHistoryRead.model_validate(row)
