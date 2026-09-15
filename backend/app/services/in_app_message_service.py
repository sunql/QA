"""站内消息 service（feat-dq-evaluation-report，Phase 7c）。

收件箱存储 + 读取/标已读。无外部推送（邮件 / IM），只服务站内「铃铛」通知。
被 ``EvaluationReportSchedulerService.run_one`` 调，给 schedule.recipients 发
「报告已生成」提醒。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import InAppMessage


class InAppMessageService:
    """站内消息 CRUD。"""

    async def create_message(
        self,
        session: AsyncSession,
        *,
        recipient_user_id: str,
        title: str,
        body: str,
        link_url: str | None = None,
    ) -> InAppMessage:
        """新增一条消息。``recipient_user_id`` 必须非空（不让系统消息混进个人铃铛）。"""
        if not recipient_user_id:
            raise ValueError("recipient_user_id must be non-empty")
        msg = InAppMessage(
            recipient=recipient_user_id,
            title=title,
            body=body,
            link_url=link_url,
            created_at=datetime.now(UTC),
        )
        session.add(msg)
        await session.flush()
        return msg

    async def list_for_user(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[InAppMessage]:
        """查某 user 的消息；``unread_only=True`` 只返回未读。"""
        stmt = (
            select(InAppMessage)
            .where(InAppMessage.recipient == user_id)
            .order_by(InAppMessage.created_at.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(InAppMessage.read_at.is_(None))
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def unread_count(
        self,
        session: AsyncSession,
        *,
        user_id: str,
    ) -> int:
        """未读计数（用于前端铃铛 badge）。"""
        stmt = (
            select(func.count())
            .select_from(InAppMessage)
            .where(
                InAppMessage.recipient == user_id,
                InAppMessage.read_at.is_(None),
            )
        )
        return int((await session.execute(stmt)).scalar_one())

    async def mark_read(
        self,
        session: AsyncSession,
        *,
        message_id: int,
        user_id: str,
    ) -> bool:
        """标已读。仅当消息属于该 user 时返回 True；否则 False（不动）。"""
        stmt = (
            update(InAppMessage)
            .where(
                InAppMessage.id == message_id,
                InAppMessage.recipient == user_id,
                InAppMessage.read_at.is_(None),
            )
            .values(read_at=datetime.now(UTC))
            .returning(InAppMessage.id)
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.scalar_one_or_none() is not None


def get_in_app_message_service() -> InAppMessageService:
    return InAppMessageService()