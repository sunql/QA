"""业务对象 REST 端点 (Phase 4.4)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    BusinessObjectCodeType,
    BusinessObjectCreate,
    BusinessObjectRead,
    BusinessObjectUpdate,
)
from app.services.business_object_service import BusinessObjectService

router = APIRouter(
    prefix="/business-objects",
    tags=["business-object"],
    dependencies=[Depends(getCurrentUser)],
)


def getBusinessObjectService() -> BusinessObjectService:
    return BusinessObjectService()


@router.get("", response_model=list[BusinessObjectRead])
async def listBusinessObjects(
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> list[BusinessObjectRead]:
    rows = await service.listObjects(session)
    return [await service.buildRead(session, r) for r in rows]


@router.get("/{code}", response_model=BusinessObjectRead)
async def getBusinessObject(
    code: BusinessObjectCodeType = Path(...),
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> BusinessObjectRead:
    row = await service.getObject(session, code)
    return await service.buildRead(session, row)


@router.post(
    "", response_model=BusinessObjectRead, status_code=status.HTTP_201_CREATED
)
async def createBusinessObject(
    payload: BusinessObjectCreate,
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
    user: CurrentUser = Depends(getCurrentUser),
) -> BusinessObjectRead:
    row = await service.createObject(session, payload, actor=user.userId)
    return await service.buildRead(session, row)


@router.put("/{code}", response_model=BusinessObjectRead)
async def updateBusinessObject(
    payload: BusinessObjectUpdate,
    code: BusinessObjectCodeType = Path(...),
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> BusinessObjectRead:
    row = await service.updateObject(session, code, payload)
    return await service.buildRead(session, row)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteBusinessObject(
    code: BusinessObjectCodeType = Path(...),
    session: AsyncSession = Depends(getDb),
    service: BusinessObjectService = Depends(getBusinessObjectService),
) -> None:
    await service.deleteObject(session, code)
