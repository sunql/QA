"""session_history_service.channel 过滤测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.session_history_service import SessionHistoryService


@pytest.mark.asyncio
async def test_listChatSessions_filters_by_channel() -> None:
    """channel='doc_qa' 时主聚合 SQL 必须含 channel='doc_qa' 过滤。"""
    svc = SessionHistoryService()
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
    )

    await svc.listChatSessions(session, limit=10, offset=0, channel="doc_qa")

    # 取首次 session.execute 调用 —— 主聚合 SELECT 是带 channel 过滤的那条；
    # 后续 _buildLastMessageContentMap 调用不带 channel（按 session_ids IN 过滤）。
    stmt = session.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "channel = 'doc_qa'" in compiled


@pytest.mark.asyncio
async def test_listChatSessions_default_channel_is_chat() -> None:
    """未传 channel 时默认 'chat'（向后兼容既有聊天行为）。"""
    svc = SessionHistoryService()
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=lambda: MagicMock(all=lambda: []))
    )

    await svc.listChatSessions(session, limit=10, offset=0)

    stmt = session.execute.call_args_list[0].args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "channel = 'chat'" in compiled