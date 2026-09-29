"""站内消息 REST API（feat-dq-evaluation-report，Phase 7c）。

挂在 /api/v1/messages：
  GET    /api/v1/messages?unreadOnly=true&limit=20   当前用户的消息列表
  POST   /api/v1/messages/{id}/read                  标已读
  GET    /api/v1/messages/unread-count               未读计数（铃铛 badge）

前端 ``MessageBell`` 30s 轮询 unread-count；点开 ``MessageDropdown`` 调 list。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.models import InAppMessage
from app.services.in_app_message_service import (
    InAppMessageService,
    get_in_app_message_service,
)

router = APIRouter()


def _to_dict(msg: InAppMessage) -> dict:
    return {
        "id": msg.id,
        "recipient": msg.recipient,
        "title": msg.title,
        "body": msg.body,
        "linkUrl": msg.link_url,
        "createdAt": msg.created_at.isoformat() if msg.created_at else None,
        "readAt": msg.read_at.isoformat() if msg.read_at else None,
    }


@router.get("")
async def listMyMessages(
    user: CurrentUser = Depends(getCurrentUser),
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    limit: int = Query(default=20, ge=1, le=200),
    service: InAppMessageService = Depends(get_in_app_message_service),
    session: AsyncSession = Depends(getDb),
) -> list[dict]:
    rows = await service.list_for_user(
        session, user_id=user.userId, unread_only=unread_only, limit=limit,
    )
    return [_to_dict(r) for r in rows]


@router.get("/unread-count")
async def getMyUnreadCount(
    user: CurrentUser = Depends(getCurrentUser),
    service: InAppMessageService = Depends(get_in_app_message_service),
    session: AsyncSession = Depends(getDb),
) -> dict:
    n = await service.unread_count(session, user_id=user.userId)
    return {"unreadCount": n}


@router.post("/{message_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def markMyMessageRead(
    message_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    service: InAppMessageService = Depends(get_in_app_message_service),
    session: AsyncSession = Depends(getDb),
) -> None:
    ok = await service.mark_read(
        session, message_id=message_id, user_id=user.userId,
    )
    if not ok:
        from app.exceptions import NotFoundError

        raise NotFoundError(message=f"message {message_id} not found")
    from fastapi import Response as _Resp

    return _Resp(status_code=status.HTTP_204_NO_CONTENT)