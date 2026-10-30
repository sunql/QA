"""数据域 REST 端点（feat-data-domain-layer 方案 1）."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    DataDomainCreate,
    DataDomainRead,
    DataDomainUpdate,
)
from app.services.data_domain_service import DataDomainService

router = APIRouter(
    tags=["data-domain"],
    dependencies=[Depends(getCurrentUser)],
)


def getDataDomainService() -> DataDomainService:
    return DataDomainService()


@router.get("", response_model=list[DataDomainRead])
async def listDataDomains(
    enabledOnly: bool = False,
    session: AsyncSession = Depends(getDb),
    service: DataDomainService = Depends(getDataDomainService),
) -> list[DataDomainRead]:
    """全量（含禁用，供管理页）；enabledOnly=true 供下拉（Agent 表单等）。"""
    return await service.listDomains(session, enabledOnly=enabledOnly)


@router.get("/{code}", response_model=DataDomainRead)
async def getDataDomain(
    code: str = Path(..., min_length=1, max_length=20),
    session: AsyncSession = Depends(getDb),
    service: DataDomainService = Depends(getDataDomainService),
) -> DataDomainRead:
    return await service.getDomainRead(session, code)


@router.post("", response_model=DataDomainRead, status_code=status.HTTP_201_CREATED)
async def createDataDomain(
    payload: DataDomainCreate,
    session: AsyncSession = Depends(getDb),
    service: DataDomainService = Depends(getDataDomainService),
    user: CurrentUser = Depends(getCurrentUser),
) -> DataDomainRead:
    return await service.createDomain(session, payload, actor=user.userId)


@router.put("/{code}", response_model=DataDomainRead)
async def updateDataDomain(
    payload: DataDomainUpdate,
    code: str = Path(..., min_length=1, max_length=20),
    session: AsyncSession = Depends(getDb),
    service: DataDomainService = Depends(getDataDomainService),
) -> DataDomainRead:
    return await service.updateDomain(session, code, payload)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteDataDomain(
    code: str = Path(..., min_length=1, max_length=20),
    session: AsyncSession = Depends(getDb),
    service: DataDomainService = Depends(getDataDomainService),
) -> None:
    await service.deleteDomain(session, code)
