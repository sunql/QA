"""audit_log 查询 API（Phase 4.5 治理 API）。

GET /api/v1/audit  # 全局查询（支持 entity_type/action/actor 过滤）
GET /api/v1/audit/{id}  # 按 ID 查单条
GET /api/v1/audit/by-entity/{entity_type}/{entity_id}  # 按实体查
GET /api/v1/audit/by-actor/{actor}  # 按用户查

不提供 POST/PUT/DELETE（audit_log 不可变）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse
import csv
import io
import json
from datetime import datetime

from app.dependencies import CurrentUser, getAdminOnlyActor, getDb
from app.domain.schemas import AuditLogPage, AuditLogRead
from app.services.audit_service import AuditService

router = APIRouter()
_audit = AuditService()
EXPORT_MAX = 100_000


async def _stream_csv(rows_gen) -> StreamingResponse:
    """Stream audit logs as CSV. Uses csv.writer for proper RFC 4180 escaping."""
    header = ["id", "created_at", "entity_type", "entity_id", "action",
              "actor", "actor_departments", "before_json", "after_json"]

    async def gen():
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate()
        async for row in rows_gen:
            writer.writerow([
                row.id,
                row.created_at.isoformat() if row.created_at else "",
                row.entity_type or "",
                row.entity_id,
                row.action or "",
                row.actor or "",
                row.actor_departments or "",
                json.dumps(row.before_json) if row.before_json is not None else "",
                json.dumps(row.after_json) if row.after_json is not None else "",
            ])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()

    return StreamingResponse(
        gen(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )


async def _stream_json(rows_gen) -> StreamingResponse:
    async def gen():
        async for row in rows_gen:
            yield json.dumps({
                "id": row.id,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
                "entityType": row.entity_type,
                "entityId": row.entity_id,
                "action": row.action,
                "actor": row.actor,
                "actorDepartments": row.actor_departments,
                "beforeJson": row.before_json,
                "afterJson": row.after_json,
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'},
    )


@router.get(
    "",
    response_model=AuditLogPage,
    status_code=status.HTTP_200_OK,
    summary="查询审计日志（全局，支持过滤）",
)
async def listAuditLogs(
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
    entity_type: Annotated[str | None, Query(description="实体类型过滤")] = None,
    action: Annotated[str | None, Query(description="动作过滤：CREATE/UPDATE/DELETE")] = None,
    actor: Annotated[str | None, Query(description="用户 ID 模糊过滤")] = None,
    entity_id: Annotated[str | None, Query(description="实体 ID 模糊过滤")] = None,
    actor_departments: Annotated[str | None, Query(description="部门模糊过滤")] = None,
    since: Annotated[str | None, Query(description="起始时间 ISO8601")] = None,
    until: Annotated[str | None, Query(description="结束时间 ISO8601")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditLogPage:
    from datetime import datetime, timezone
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    rows, total = await _audit.listAll(
        db,
        entity_type=entity_type,
        action=action,
        actor=actor,
        entity_id=entity_id,
        actor_departments=actor_departments,
        since=since_dt,
        until=until_dt,
        limit=limit,
        offset=offset,
    )
    return AuditLogPage(rows=[AuditLogRead.model_validate(r) for r in rows], total=total)


@router.get(
    "/export",
    summary="流式导出审计日志（admin only，最大 100k 行）",
)
async def exportAuditLogs(
    format: Annotated[Literal["csv", "json"], Query()] = "csv",
    entity_type: Annotated[str | None, Query(description="实体类型过滤")] = None,
    action: Annotated[str | None, Query(description="动作过滤：CREATE/UPDATE/DELETE")] = None,
    actor: Annotated[str | None, Query(description="用户 ID 模糊过滤")] = None,
    actor_departments: Annotated[str | None, Query(description="部门模糊过滤")] = None,
    since: Annotated[str | None, Query(description="起始时间 ISO8601")] = None,
    until: Annotated[str | None, Query(description="结束时间 ISO8601")] = None,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    from datetime import datetime
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None
    rows_gen = _audit.iterAll(
        db,
        entity_type=entity_type,
        action=action,
        actor=actor,
        actor_departments=actor_departments,
        since=since_dt,
        until=until_dt,
        max_rows=EXPORT_MAX,
    )
    if format == "csv":
        return await _stream_csv(rows_gen)
    return await _stream_json(rows_gen)


@router.get(
    "/by-entity/{entity_type}/{entity_id}",
    response_model=list[AuditLogRead],
    status_code=status.HTTP_200_OK,
    summary="按实体查询审计记录",
)
async def listByEntity(
    entity_type: str,
    entity_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    rows = await _audit.listByEntity(
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
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditLogRead]:
    rows = await _audit.listByActor(
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
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
) -> AuditLogRead:
    row = await _audit.getById(db, id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="审计记录不存在")
    return AuditLogRead.model_validate(row)
