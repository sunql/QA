"""验证 Alembic 0047 后 session_message 新列/索引落地。"""
import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import getSettings
from app.domain.models import SessionMessage


@pytest.mark.asyncio
async def test_session_message_has_new_columns() -> None:
    """Alembic 0047 后 session_message 必须含 channel/citations/user_id 三列。"""

    def _columns(sync_conn):
        insp = inspect(sync_conn)
        return {c["name"] for c in insp.get_columns("session_message")}

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl)
    async with engine.begin() as conn:
        cols = await conn.run_sync(_columns)
    await engine.dispose()
    assert "channel" in cols
    assert "citations" in cols
    assert "user_id" in cols


@pytest.mark.asyncio
async def test_session_message_has_new_indexes() -> None:
    """新索引必须存在以支撑 channel + user_id 查询。"""

    def _indexes(sync_conn):
        insp = inspect(sync_conn)
        return {i["name"] for i in insp.get_indexes("session_message")}

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl)
    async with engine.begin() as conn:
        idx = await conn.run_sync(_indexes)
    await engine.dispose()
    assert "idx_session_msg_channel_time" in idx
    assert "idx_session_msg_user_channel_time" in idx