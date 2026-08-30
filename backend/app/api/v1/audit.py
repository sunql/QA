"""audit_log 查询 API（Phase 4.5 治理 API）。

GET /api/v1/audit  # 全局查询（支持 entity_type/action/actor 过滤）
GET /api/v1/audit/{id}  # 按 ID 查单条
GET /api/v1/audit/by-entity/{entity_type}/{entity_id}  # 按实体查
GET /api/v1/audit/by-actor/{actor}  # 按用户查

不提供 POST/PUT/DELETE（audit_log 不可变）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import AuditLogRead
from app.services.audit_service import AuditService

router = APIRouter()
_service = AuditService()


@router.get(
    "",
    response_model=list[AuditLogRead],
    status_code=status.HTTP_200_OK,
    summary="查询审计日志（全局，支持过滤）",
)
async def listAuditLogs(
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    entity_type: Annotated[str | None, Query(description="实体类型过滤")] = None,
    action: Annotated[str | None, Query(description="动作过滤：CREATE/UPDATE/DELETE")] = None,
    actor: Annotated[str | None, Query(description="用户 ID 过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    rows = await _service.listAll(
        db,
        entity_type=entity_type,
        action=action,
        actor=actor,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(r) for r in rows]


@router.get(
    "/by-entity/{entity_type}/{entity_id}",
    response_model=list[AuditLogRead],
    status_code=status.HTTP_200_OK,
    summary="按实体查询审计记录",
)
async def listByEntity(
    entity_type: str,
    entity_id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    rows = await _service.listByEntity(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(r) for r in rows]


@router.get(
    "/by-actor/{actor}",
    response_model=list[AuditLogRead],
    status_code=status.HTTP_200_OK,
    summary="按用户查询审计记录",
)
async def listByActor(
    actor: str,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    rows = await _service.listByActor(
        db,
        actor=actor,
        limit=limit,
        offset=offset,
    )
    return [AuditLogRead.model_validate(r) for r in rows]


@router.get(
    "/{id}",
    response_model=AuditLogRead,
    status_code=status.HTTP_200_OK,
    summary="按 ID 查询单条审计记录",
)
async def getAuditLog(
    id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> AuditLogRead:
    row = await _service.getById(db, id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="审计记录不存在")
    return AuditLogRead.model_validate(row)
