"""id_mapping API — v3.1 M0-P0.1。

挂在 /api/v1/id-mappings：
  GET    /api/v1/id-mappings                        列表（按 businessObject 过滤）
  POST   /api/v1/id-mappings                        创建（201）
  GET    /api/v1/id-mappings/{unified_id}           详情
  PUT    /api/v1/id-mappings/{unified_id}          更新三库 ID
  DELETE /api/v1/id-mappings/{unified_id}           删除（204）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.schemas import (
    IdMappingCreate,
    IdMappingRead,
    IdMappingUpdate,
)
from app.services.id_mapping_service import IdMappingService


router = APIRouter(prefix="/id-mappings", tags=["id-mapping"])


def _svc() -> IdMappingService:
    return IdMappingService()


@router.get("", response_model=list[IdMappingRead])
async def listIdMappings(
    _user: CurrentUser = Depends(getCurrentUser),
    businessObject: str | None = Query(default=None, alias="businessObject"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session=Depends(getDb),
    svc: IdMappingService = Depends(_svc),
) -> list[IdMappingRead]:
    if businessObject:
        items, _ = await svc.listByBusinessObject(session, businessObject, limit, offset)
    else:
        # 无过滤时返回全部（按 unified_id 分页）
        from sqlalchemy import select, func
        from app.domain.models import IdMapping
        total_q = select(func.count()).select_from(IdMapping)
        total = (await session.execute(total_q)).scalar_one()
        rows = await session.execute(
            select(IdMapping).order_by(IdMapping.unified_id).limit(limit).offset(offset)
        )
        items = [IdMappingRead.model_validate(m) for m in rows.scalars().all()]
    return items


@router.post(
    "",
    response_model=IdMappingRead,
    status_code=status.HTTP_201_CREATED,
    summary="注册统一 ID 映射",
)
async def createIdMapping(
    payload: IdMappingCreate,
    _user: CurrentUser = Depends(getCurrentUser),
    session=Depends(getDb),
    svc: IdMappingService = Depends(_svc),
) -> IdMappingRead:
    try:
        return await svc.register(session, payload)
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message)


@router.get("/{unified_id}", response_model=IdMappingRead)
async def getIdMapping(
    unified_id: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session=Depends(getDb),
    svc: IdMappingService = Depends(_svc),
) -> IdMappingRead:
    try:
        return await svc.resolve(session, unified_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message)


@router.put("/{unified_id}", response_model=IdMappingRead)
async def updateIdMapping(
    unified_id: str,
    payload: IdMappingUpdate,
    _user: CurrentUser = Depends(getCurrentUser),
    session=Depends(getDb),
    svc: IdMappingService = Depends(_svc),
) -> IdMappingRead:
    try:
        return await svc.update(session, unified_id, payload)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message)


@router.delete("/{unified_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteIdMapping(
    unified_id: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session=Depends(getDb),
    svc: IdMappingService = Depends(_svc),
) -> None:
    try:
        await svc.delete(session, unified_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message)
