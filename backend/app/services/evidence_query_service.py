"""evidence_query_service — read-only DB helpers for /evidences endpoints.

Pure query layer; no business logic, no LLM. Implemented with explicit
SQLAlchemy select() (not autoload) so filter combinations are visible.

Why split from API: the API layer should only deal with HTTP parsing and
Pydantic validation. This service holds the WHERE-clause composition that
multiple endpoints reuse (list, by-session).

R2 归属收敛（security H2）：session 归属事实源 = session_message.user_id
（chat 行由入口 contextvar 打标，见 evidence_record_service）。守卫语义与
wiki.py wiki_qa / documents.py docQa 同模式：有标记且不属于本人（非 admin）
→ 拒绝；无标记（存量/新会话）→ 放行。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SessionMessage
from app.domain.wiki_models import Evidence
from app.domain.wiki_schemas import EvidenceQuery

_CHAT_CHANNEL = "chat"


async def getSessionOwnerUserIds(
    session: AsyncSession, sessionId: str, *, channel: str | None = _CHAT_CHANNEL
) -> set[str]:
    """返回该 session 已标记的全部归属 user_id（server-side 事实源）。

    空集 = 无归属标记（存量 NULL 行或全新会话），由调用方决定放行
    （fail-open，与 wiki.py wiki_qa 守卫同语义）。

    ``channel=None`` = **不限渠道**：取该 session 内任意渠道已标记行的归属者并集。
    会话端点（``/sessions/{id}/messages``、``DELETE /sessions/{id}``、``export.pdf``）
    服务 chat / doc_qa / wiki_qa **三个**渠道（`_CHAT_CHANNEL` 只有 "chat"），
    而 doc_qa 行由 rag_qa_service 打标、wiki_qa 行由 wiki 侧打标 —— 端点侧沿用
    默认的 "chat" 过滤会让另外两个渠道恒返空集，守卫**静默 fail-open**。
    同一 session_id 内渠道唯一，取并集只会更严（多找到归属者→多拦），不会误判。
    """
    conditions = [
        SessionMessage.session_id == sessionId,
        SessionMessage.user_id.is_not(None),
    ]
    if channel is not None:
        conditions.append(SessionMessage.channel == channel)
    rows = await session.execute(select(SessionMessage.user_id).where(*conditions))
    return set(rows.scalars().all())


async def listEvidences(
    session: AsyncSession,
    query: EvidenceQuery,
    *,
    viewerUserId: str | None = None,
    viewerIsAdmin: bool = False,
) -> tuple[list[Evidence], int]:
    """列出证据，按 query 过滤；返回 (items, total)。

    过滤：session_id / claim_id / source_type 三者 AND；limit / offset 分页。
    total 在同一会话内用 func.count 取，不发起新事务。

    归属收敛（R2 H2）：不带 session_id 过滤时（批量枚举面），携带 session
    的证据行对非 admin 收敛为「本人拥有的 chat session」；无 session 的
    claim 挂靠行（Document 语义）不受限。带 session_id 过滤时由 API 层
    _assertSessionOwnership 做定点校验，此处不重复。
    """
    base = select(Evidence)
    if query.session_id is not None:
        base = base.where(Evidence.session_id == query.session_id)
    elif not viewerIsAdmin:
        if viewerUserId:
            ownedSessions = select(SessionMessage.session_id).where(
                SessionMessage.user_id == viewerUserId,
                SessionMessage.channel == _CHAT_CHANNEL,
            )
            base = base.where(
                or_(
                    Evidence.session_id.is_(None),
                    Evidence.session_id.in_(ownedSessions),
                )
            )
        else:
            base = base.where(Evidence.session_id.is_(None))
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
