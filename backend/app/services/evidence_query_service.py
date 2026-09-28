"""evidence_query_service — read-only DB helpers for /evidences endpoints.

Pure query layer; no business logic, no LLM. Implemented with explicit
SQLAlchemy select() (not autoload) so filter combinations are visible.

Why split from API: the API layer should only deal with HTTP parsing and
Pydantic validation. This service holds the WHERE-clause composition that
multiple endpoints reuse (list, by-session).
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence
from app.domain.wiki_schemas import EvidenceQuery


async def listEvidences(
    session: AsyncSession, query: EvidenceQuery
) -> tuple[list[Evidence], int]:
    """列出证据，按 query 过滤；返回 (items, total)。

    过滤：session_id / claim_id / source_type 三者 AND；limit / offset 分页。
    total 在同一会话内用 func.count 取，不发起新事务。
    """
    base = select(Evidence)
    if query.session_id is not None:
        base = base.where(Evidence.session_id == query.session_id)
    if query.claim_id is not None:
        base = base.where(Evidence.claim_id == query.claim_id)
    if query.source_type is not None:
        base = base.where(Evidence.source_type == query.source_type)
    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0
    page = base.order_by(Evidence.id).limit(query.limit).offset(query.offset)
    items = (await session.scalars(page)).all()
    return list(items), int(total)


async def listEvidencesBySession(
    session: AsyncSession,
    session_id: str,
    limit: int,
    offset: int,
) -> tuple[list[Evidence], int]:
    """按 chat session_id 列出证据（便捷端点，参数最少）。"""
    q = EvidenceQuery(
        session_id=session_id, limit=limit, offset=offset
    )
    return await listEvidences(session, q)


async def getEvidenceById(
    session: AsyncSession, evidence_id: int
) -> Evidence | None:
    """按主键查 evidence；不存在返回 None。"""
    return await session.scalar(
        select(Evidence).where(Evidence.id == evidence_id)
    )
