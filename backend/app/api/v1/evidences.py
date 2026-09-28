"""evidences API — v3.1 M1'.

GET /api/v1/evidences                       # 列表 + 过滤
GET /api/v1/evidences/by-session/{sid}      # 必须先于 /{evidence_id} 注册
GET /api/v1/evidences/{evidence_id}          # 详情

仅只读；写入路径（B2 业务 SQL 自动落库）不在本路由范围。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getDb
from app.domain.wiki_schemas import (
    EvidenceListOut,
    EvidenceQuery,
    EvidenceRead,
)
from app.services.evidence_query_service import (
    getEvidenceById,
    listEvidences,
    listEvidencesBySession,
)


router = APIRouter(prefix="/evidences", tags=["evidences"])


async def _evidencesSession() -> AsyncIterator[AsyncSession]:
    async for s in getDb():
        yield s


# ⚠️ 路由顺序：by-session 必须先于 /{evidence_id}（wiki search endpoint 教训）


@router.get(
    "/by-session/{sid}",
    response_model=EvidenceListOut,
    summary="按 chat session 列出证据",
)
async def listBySession(
    sid: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceListOut:
    items, total = await listEvidencesBySession(session, sid, limit, offset)
    return EvidenceListOut(
        items=[EvidenceRead.model_validate(e) for e in items],
        total=total,
    )


@router.get(
    "",
    response_model=EvidenceListOut,
    summary="列出 evidence（按 session_id / claim_id / source_type 过滤）",
)
async def listEvidence(
    q: EvidenceQuery = Depends(),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceListOut:
    items, total = await listEvidences(session, q)
    return EvidenceListOut(
        items=[EvidenceRead.model_validate(e) for e in items],
        total=total,
    )


@router.get(
    "/{evidence_id}",
    response_model=EvidenceRead,
    summary="查 evidence 详情",
)
async def getEvidence(
    evidence_id: int = Path(..., ge=1),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceRead:
    e = await getEvidenceById(session, evidence_id)
    if e is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return EvidenceRead.model_validate(e)
