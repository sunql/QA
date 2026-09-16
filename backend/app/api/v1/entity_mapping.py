"""跨系统编码映射 API 路由（Phase 3.1）。

挂在 /api/v1/entity-mappings：
  GET    /api/v1/entity-mappings                列表（按 entityType / sourceSystem / enterpriseKey 过滤）
  GET    /api/v1/entity-mappings/search         AutoComplete 搜索
  GET    /api/v1/entity-mappings/{id}           详情
  POST   /api/v1/entity-mappings                创建（201）
  POST   /api/v1/entity-mappings/bulk           批量导入（feat-entity-mapping-bulk-import）
  PUT    /api/v1/entity-mappings/{id}           更新
  DELETE /api/v1/entity-mappings/{id}           删除（204）

bulk 端点必须在 /{mappingId} 之前注册（FastAPI 路由按顺序匹配）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.enums import SourceSystem
from app.domain.schemas import (
    BusinessObjectCodeType,
    EntityMappingBulkImportItem,
    EntityMappingBulkResult,
    EntityMappingCreate,
    EntityMappingRead,
    EntityMappingSearchHit,
    EntityMappingUpdate,
)
from app.services.entity_mapping_service import (
    EntityMappingService,
    entityMappingSearchToHit,
    entityMappingToRead,
)

router = APIRouter()


def getEntityMappingService() -> EntityMappingService:
    """service 无状态依赖，直接返回新实例；保留工厂风格便于后续注入。"""
    return EntityMappingService()


@router.get("", response_model=list[EntityMappingRead])
async def listEntityMappings(
    _user: CurrentUser = Depends(getCurrentUser),
    entityType: BusinessObjectCodeType | None = Query(default=None, alias="entityType"),
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


@router.get("/search", response_model=list[EntityMappingSearchHit])
async def searchEntityMappings(
    q: str = Query(default="", min_length=0, max_length=100, description="搜索关键词：enterprise_code/source_code ILIKE + 全数字时 enterprise_key 精确"),
    entityType: BusinessObjectCodeType | None = Query(default=None, alias="entityType"),
    limit: int = Query(default=20, ge=1, le=100),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> list[EntityMappingSearchHit]:
    """Phase 6.x：AutoComplete 搜索接口。必须在 /{mappingId} 之前注册（FastAPI 路由按顺序匹配）。"""
    mappings = await service.searchMappings(
        session, q=q, entityType=entityType, limit=limit,
    )
    return [entityMappingSearchToHit(m) for m in mappings]


@router.get("/{mappingId}", response_model=EntityMappingRead)
async def getEntityMapping(
    mappingId: int,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingRead:
    mapping = await service.getMapping(session, mappingId)
    return entityMappingToRead(mapping)


@router.post("", response_model=EntityMappingRead, status_code=status.HTTP_201_CREATED)
async def createEntityMapping(
    payload: EntityMappingCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingRead:
    mapping = await service.createMapping(session, payload, user)
    return entityMappingToRead(mapping)


@router.post("/bulk", response_model=EntityMappingBulkResult)
async def bulkImportEntityMappings(
    items: list[EntityMappingBulkImportItem],
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingBulkResult:
    """批量导入跨系统编码映射（feat-entity-mapping-bulk-import 2026-09-16）。

    请求体：JSON 数组，元素为 EntityMappingBulkImportItem schema；上限 1000 行。
    enterprise_key 可省略（默认 0 → 后端按 enterprise_code + entity_type 派生）。

    响应：每行 1 条 EntityMappingBulkResultRow，含 status（inserted/updated/skipped/failed）。

    路由顺序：本端点必须在 /{mappingId} 之前注册（FastAPI 按声明顺序匹配）。
    """
    return await service.bulkImportMappings(session, items, user)


@router.put("/{mappingId}", response_model=EntityMappingRead)
async def updateEntityMapping(
    mappingId: int,
    payload: EntityMappingUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> EntityMappingRead:
    mapping = await service.updateMapping(session, mappingId, payload, user)
    return entityMappingToRead(mapping)


@router.delete("/{mappingId}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteEntityMapping(
    mappingId: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EntityMappingService = Depends(getEntityMappingService),
) -> None:
    await service.deleteMapping(session, mappingId, user)
