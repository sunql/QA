"""KPI Catalog REST API（Phase 4.1）。

与 data_quality / entity_mapping 同模式：扁平路径，挂在 /api/v1/kpi-catalog。
KPI 不进 Neo4j / Milvus（治理层）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    KpiCatalogCreate,
    KpiCatalogRead,
    KpiCatalogUpdate,
)
from app.services.kpi_catalog_service import KpiCatalogService

router = APIRouter(dependencies=[])
_service = KpiCatalogService()


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
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KpiCatalogRead:
    """创建 KPI。重复 kpi_code 返回 409。"""
    return KpiCatalogRead.model_validate(await _service.createKpi(db, dto))


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
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> KpiCatalogRead:
    """更新 KPI（revision_count +1）。"""
    return KpiCatalogRead.model_validate(await _service.updateKpi(db, id, dto))


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteKpi(
    id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除 KPI。"""
    await _service.deleteKpi(db, id)
