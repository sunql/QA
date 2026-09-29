"""evidences API — v3.1 M1'.

GET /api/v1/evidences                       # 列表 + 过滤
GET /api/v1/evidences/by-session/{sid}      # 必须先于 /{evidence_id} 注册
GET /api/v1/evidences/{evidence_id}          # 详情

仅只读；写入路径（B2 业务 SQL 自动落库）不在本路由范围。

R2 归属守卫（security H2）：任意认证用户此前可枚举他人 session 的 SQL 原文
+ result_hash。现收敛为——session 维度定点校验（_assertSessionOwnership，
归属事实源 = session_message.user_id，admin 放行，存量无标记行 fail-open
与 wiki.py wiki_qa / documents.py docQa 同语义）；无 session 过滤的列表在
service 层按 viewer 收敛（evidence_query_service.listEvidences）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.wiki_schemas import (
    EvidenceListOut,
    EvidenceQuery,
    EvidenceRead,
)
from app.services.evidence_query_service import (
    getEvidenceById,
    getSessionOwnerUserIds,
    listEvidences,
    listEvidencesBySession,
)
from app.services.messages_zh import MSG_EVIDENCE_SESSION_NOT_OWNED

router = APIRouter(prefix="/evidences", tags=["evidences"])


async def _evidencesSession() -> AsyncIterator[AsyncSession]:
    async for s in getDb():
        yield s


async def _assertSessionOwnership(
    session: AsyncSession, sessionId: str, user: CurrentUser
) -> None:
    """session 归属定点校验（R2 H2）。

    - admin 放行（getAdminOnlyActor 同判据："admin" in user.roles）
    - session 已有归属标记且不属于当前用户 → 403（detail 不回显归属者，
      防 403 侧信道枚举归属人）
    - 无标记（存量 NULL 行 / 全新会话）→ 放行（fail-open，wiki_qa 同语义）
    - actor 由服务端 getCurrentUser 派生，不读任何客户端自报字段
    """
    if "admin" in (user.roles or []):
        return
    owners = await getSessionOwnerUserIds(session, sessionId)
    if owners and user.userId not in owners:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=MSG_EVIDENCE_SESSION_NOT_OWNED,
        )


# ⚠️ 路由顺序：by-session 必须先于 /{evidence_id}（wiki search endpoint 教训）


@router.get(
    "/by-session/{sid}",
    response_model=EvidenceListOut,
    summary="按 chat session 列出证据",
)
async def listBySession(
    _user: CurrentUser = Depends(getCurrentUser),
    sid: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceListOut:
    await _assertSessionOwnership(session, sid, _user)
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
    _user: CurrentUser = Depends(getCurrentUser),
    q: EvidenceQuery = Depends(),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceListOut:
    if q.session_id is not None:
        await _assertSessionOwnership(session, q.session_id, _user)
    items, total = await listEvidences(
        session,
        q,
        viewerUserId=_user.userId,
        viewerIsAdmin="admin" in (_user.roles or []),
    )
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
    _user: CurrentUser = Depends(getCurrentUser),
    evidence_id: int = Path(..., ge=1),
    session: AsyncSession = Depends(_evidencesSession),
) -> EvidenceRead:
    e = await getEvidenceById(session, evidence_id)
    if e is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if e.session_id is not None:
        await _assertSessionOwnership(session, e.session_id, _user)
    return EvidenceRead.model_validate(e)
