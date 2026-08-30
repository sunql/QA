"""跨系统编码映射 API 路由（Phase 3.1）。

挂在 /api/v1/entity-mappings：
  GET    /api/v1/entity-mappings                列表（按 entityType / sourceSystem / enterpriseKey 过滤）
  GET    /api/v1/entity-mappings/{id}           详情
  POST   /api/v1/entity-mappings                创建（201）
  PUT    /api/v1/entity-mappings/{id}           更新
  DELETE /api/v1/entity-mappings/{id}           删除（204）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser, getDb
from app.domain.enums import EntityType, SourceSystem
from app.domain.schemas import EntityMappingCreate, EntityMappingRead, EntityMappingUpdate
from app.services.entity_mapping_service import (
    EntityMappingService,
    entityMappingToRead,
)

router = APIRouter(dependencies=[Depends(getCurrentUser)])


def getEntityMappingService() -> EntityMappingService:
    """service 无状态依赖，直接返回新实例；保留工厂风格便于后续注入。"""
    return EntityMappingService()


@router.get("", response_model=list[EntityMappingRead])
async def listEntityMappings(
    entityType: EntityType | None = Query(default=None, alias="entityType"),
    sourceSystem: SourceSystem | None = Query(default=None, alias="sourceSystem"),
    enterpriseKey: int | None = Query(default=None, alias="enterpriseKey"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> list[EntityMappingRead]:
    mappings = await service.listMappings(
        session,
        entityType=entityType,
        sourceSystem=sourceSystem,
        enterpriseKey=enterpriseKey,
        limit=limit,
        offset=offset,
    )
    return [entityMappingToRead(m) for m in mappings]


@router.get("/{mappingId}", response_model=EntityMappingRead)
async def getEntityMapping(
    mappingId: int,
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingRead:
    mapping = await service.getMapping(session, mappingId)
    return entityMappingToRead(mapping)


@router.post("", response_model=EntityMappingRead, status_code=status.HTTP_201_CREATED)
async def createEntityMapping(
    payload: EntityMappingCreate,
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingRead:
    mapping = await service.createMapping(session, payload)
    return entityMappingToRead(mapping)


@router.put("/{mappingId}", response_model=EntityMappingRead)
async def updateEntityMapping(
    mappingId: int,
    payload: EntityMappingUpdate,
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingRead:
    mapping = await service.updateMapping(session, mappingId, payload)
    return entityMappingToRead(mapping)


@router.delete("/{mappingId}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteEntityMapping(
    mappingId: int,
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> None:
    await service.deleteMapping(session, mappingId)
