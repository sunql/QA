"""验证 Alembic 0047 / 0085 后 session_message 新列/索引落地。"""
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


@pytest.mark.asyncio
async def test_session_message_interrupted_column_contract() -> None:
    """Alembic 0085（H4 断连兜底）后 interrupted 必须为 NOT NULL DEFAULT false。

    三件事一起钉死，缺一即漂移：
    1. 列存在且 `nullable=False` —— H4 的判定是布尔读（`r.interrupted`），可空列会让
       历史行读成 None 而前端 `interrupted: boolean`（必填）静默拿不到值；
    2. 库侧默认 `false` —— 636 行历史行是加列时回填的，不是写路径补的，故默认值本身
       就是契约（老版本代码写入的行也必须读成「未中断」）；
    3. ORM 与库一致 —— 本文件直连 prod `settings.databaseUrl` 取实际 schema，
       正是为了拦住「迁移跑了但 ORM 没加列」这类只在运行期才炸的漂移。
    """
    from sqlalchemy import inspect as sa_inspect

    def _col(sync_conn):
        insp = sa_inspect(sync_conn)
        for c in insp.get_columns("session_message"):
            if c["name"] == "interrupted":
                return c
        return None

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl)
    async with engine.begin() as conn:
        col = await conn.run_sync(_col)
    await engine.dispose()

    assert col is not None, "session_message.interrupted 列不存在（0085 未应用？）"
    assert col["nullable"] is False, f"interrupted 必须 NOT NULL，实际 nullable={col['nullable']}"
    assert str(col["default"]).strip().lower() in {"false", "'false'::boolean"}, (
        f"interrupted 库侧默认值应为 false，实际 {col['default']!r}"
    )

    orm_col = SessionMessage.__table__.columns["interrupted"]
    assert orm_col.nullable is False, "ORM 侧 interrupted 必须 nullable=False（与库一致）"