"""验证 Alembic 0047 / 0085 / 0105 后 session_message 新列/索引落地。"""
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


@pytest.mark.asyncio
async def test_session_message_chart_columns_contract() -> None:
    """Alembic 0105（图表进最终报告）后 chart_type / chart_option 落地。

    两列都必须 **nullable**：0105 之前的存量行没有图，加列时不可能回填出有意义的值；
    强行 NOT NULL 只能填个假值，而「这轮没有图」与「这轮有张空图」是两回事。
    同时钉 ORM 与库一致 —— 迁移跑了但 ORM 没加列，正是运行期才炸的那类漂移。
    """
    def _cols(sync_conn):
        return {c["name"]: c for c in inspect(sync_conn).get_columns("session_message")}

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl)
    async with engine.begin() as conn:
        cols = await conn.run_sync(_cols)
    await engine.dispose()

    assert "chart_type" in cols, "session_message.chart_type 列不存在（0105 未应用？）"
    assert "chart_option" in cols, "session_message.chart_option 列不存在（0105 未应用？）"
    for name in ("chart_type", "chart_option"):
        assert cols[name]["nullable"] is True, f"{name} 必须可空（存量行无图）"
        assert SessionMessage.__table__.columns[name].nullable is True, (
            f"ORM 侧 {name} 必须与库一致为可空"
        )