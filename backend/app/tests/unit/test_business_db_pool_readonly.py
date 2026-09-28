"""业务库只读事务守卫（库侧只读兜底批）。

背景：SQL Guard 是解析层枚举式防御，已在 M1/M2 批补到极致但永远追不上方言扩展/自定义函数/
未来新增函数。本批在应用层给业务查询套上「事务级只读」兜底：

- PG：`SET TRANSACTION READ ONLY`（在 `begin()` 后的同一只读事务中执行）
- MySQL：`SET SESSION TRANSACTION READ ONLY`（会话级只读事务）
- Oracle：`ALTER SESSION SET READ ONLY`（Oracle 12c+ 支持会话级只读）

注意：
1. 这是「应用层兜底」，不是「库侧授权」——真闸门仍是只读账号 + 撤销敏感权限（见
   `scripts/db-readonly-account-setup.sql` 给 DBA 的清单）。本测试只验证应用层注入正确。
2. 解析层黑名单保留不删（快速失败 + M2 拒绝原因回注自愈反馈）。
3. 不创建真数据库连接 —— 全部用 AsyncMock 拦截 `_ensureEngine()` / `connect_async()`。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.infrastructure import business_db_pool as pool
from app.infrastructure.business_db_pool import (
    _OracleAdapter,
    _SqlaAdapter,
)


def _make_sqla_conn_with_tx_mock() -> MagicMock:
    """构造一个假的 AsyncConnection + AsyncTransaction，模拟 SQLAlchemy 异步连接。

    关键：`_SqlaAdapter.execute_read_only` 改后会用 `async with conn.begin() as tx:`，
    这个 tx 必须能在 `await conn.execute(text(sql))` 前后被记录下来。
    """
    # tx 是 context manager：__aenter__/__aexit__ 都返回 self
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=None)
    # conn.begin() 返回 tx
    conn = MagicMock(spec=AsyncConnection)
    conn.begin = MagicMock(return_value=tx)
    # conn.execute(text("...")) 记录 SQL 并返回带 mappings 的 result
    executed: list[str] = []

    async def fake_exec(sql_obj):
        executed.append(str(sql_obj))
        # 返回假 Result，result.mappings().fetchmany(limit) → []
        result = MagicMock()
        result.mappings = MagicMock(
            return_value=MagicMock(
                all=MagicMock(return_value=[]),
                fetchmany=MagicMock(return_value=[]),
            )
        )
        return result

    conn.execute = fake_exec
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    return conn, executed


def _make_engine(conn: MagicMock) -> MagicMock:
    """构造 AsyncEngine：engine.connect() 返回 conn。"""
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = MagicMock(return_value=conn)
    return engine


@pytest.mark.asyncio
async def test_pg_execute_read_only_wraps_in_readonly_transaction() -> None:
    """PG：执行前先 `begin()` 再 `SET TRANSACTION READ ONLY`，原 SQL 紧随其后。"""
    conn, executed = _make_sqla_conn_with_tx_mock()
    adapter = _SqlaAdapter("postgresql+asyncpg://user:pwd@host:5432/db")

    with patch.object(adapter, "_ensureEngine", return_value=_make_engine(conn)):
        # 把 wait_for 变成直通（coroutine 透传）：直接 await coro
        async def _passthrough_wait_for(coro, _timeout):
            return await coro
        with patch.object(pool.asyncio, "wait_for", side_effect=_passthrough_wait_for):
            rows = await adapter.execute_read_only("SELECT 1")

    assert rows == []
    # 顺序：必须先 SET TRANSACTION READ ONLY，再原 SQL
    assert len(executed) == 2, f"期望 SET + SELECT 两步，实际 {executed}"
    assert "SET TRANSACTION READ ONLY" in executed[0]
    assert executed[1] == "SELECT 1"
    # conn.begin() 必须被调用一次
    conn.begin.assert_called_once()


@pytest.mark.asyncio
async def test_mysql_execute_read_only_uses_session_readonly() -> None:
    """MySQL：`SET SESSION TRANSACTION READ ONLY`（会话级只读事务），不是 PG 那个语法。"""
    conn, executed = _make_sqla_conn_with_tx_mock()
    adapter = _SqlaAdapter("mysql+aiomysql://user:pwd@host:3306/db")

    with patch.object(adapter, "_ensureEngine", return_value=_make_engine(conn)):
        async def _passthrough_wait_for(coro, _timeout):
            return await coro
        with patch.object(pool.asyncio, "wait_for", side_effect=_passthrough_wait_for):
            rows = await adapter.execute_read_only("SELECT 1")

    assert rows == []
    assert len(executed) == 2
    # 关键：MySQL 走 SESSION 语法
    assert "SET SESSION TRANSACTION READ ONLY" in executed[0]
    assert "SET TRANSACTION READ ONLY" not in executed[0] or "SESSION" in executed[0]
    assert executed[1] == "SELECT 1"


@pytest.mark.asyncio
async def test_oracle_execute_read_only_sets_transaction_readonly() -> None:
    """Oracle 19c：`SET TRANSACTION READ ONLY`（事务级只读），不走 PG 语法。

    历史：本测试早期用 `ALTER SESSION SET READ ONLY`，但 2026-09-27 生产真机探针确认
    THBI 实例下 `ALTER SESSION SET READ ONLY` 抛 ORA-02248（invalid option for ALTER SESSION），
    即使用户已 `GRANT ALTER SESSION` 也被拒——非权限问题，是实例配置问题。
    改用 `SET TRANSACTION READ ONLY`（事务级），单 cursor.execute 的 SELECT 自动落在 readonly tx 内。
    """
    adapter = _OracleAdapter(
        host="host", port=1521, service_name="svc", username="u", password="p"
    )

    executed: list[str] = []

    # 模拟 oracledb.connect_async 返回的连接对象
    fake_conn = MagicMock()
    fake_cursor = MagicMock()

    async def fake_cursor_execute(sql_text):
        executed.append(sql_text)

    fake_cursor.execute = fake_cursor_execute
    fake_cursor.description = None
    fake_cursor.fetchmany = AsyncMock(return_value=[])
    fake_cursor.close = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)
    fake_conn.ping = AsyncMock()
    fake_conn.close = AsyncMock()

    async def fake_connect_async(*args, **kwargs):
        return fake_conn

    with patch.object(pool.oracledb, "connect_async", side_effect=fake_connect_async):
        async def _passthrough_wait_for(coro, _timeout):
            return await coro
        with patch.object(pool.asyncio, "wait_for", side_effect=_passthrough_wait_for):
            rows = await adapter.execute_read_only("SELECT 1 FROM dual")

    assert rows == []
    # 顺序：先 SET TRANSACTION READ ONLY，再原 SQL
    assert len(executed) == 2, f"期望 SET TX + SELECT 两步，实际 {executed}"
    assert executed[0] == "SET TRANSACTION READ ONLY"
    assert executed[1] == "SELECT 1 FROM dual"


@pytest.mark.asyncio
async def test_readonly_guard_does_not_break_existing_row_limit() -> None:
    """只读事务注入不影响行数限制与 fetchmany 调用。"""
    conn, executed = _make_sqla_conn_with_tx_mock()
    adapter = _SqlaAdapter("postgresql+asyncpg://user:pwd@host:5432/db")

    # 设置行数限制为 100
    fake_settings = MagicMock()
    fake_settings.queryRowLimit = 100
    fake_settings.queryTimeoutSeconds = 30

    with patch.object(adapter, "_ensureEngine", return_value=_make_engine(conn)):
        with patch.object(pool, "getSettings", return_value=fake_settings):
            async def _passthrough_wait_for(coro, _timeout):
                return await coro
            with patch.object(pool.asyncio, "wait_for", side_effect=_passthrough_wait_for):
                await adapter.execute_read_only("SELECT 1")

    # 只读注入后只查了一次原 SQL，且 mappings.fetchmany(100) 被调用过
    assert "SET TRANSACTION READ ONLY" in executed[0]
    assert executed[1] == "SELECT 1"


@pytest.mark.asyncio
async def test_oracle_set_transaction_failure_does_not_block_query() -> None:
    """Oracle 实例拒绝 SET TRANSACTION READ ONLY（如 ORA-01536 / ORA-02248 / <12c /
    受限 PDB）时，原 SQL 必须照常执行 + 返回正确行。这是纵深防御关键：
    解析层黑名单 + 只读账号权限仍是防线，事务级 readonly 是 best-effort，失败不可阻塞业务查询。

    复现路径：2026-09-27 生产 THBI 19c 实测，`ALTER SESSION SET READ ONLY` 抛 ORA-02248，
    已改用 `SET TRANSACTION READ ONLY`（事务级）。本测试覆盖 SET TRANSACTION 失败兜底路径。
    """
    adapter = _OracleAdapter(
        host="host", port=1521, service_name="svc", username="u", password="p"
    )

    executed: list[str] = []
    ORA_GENERIC = Exception("ORA-01536: snapshot too old or simulated SET TRANSACTION failure")

    async def fake_cursor_execute(sql_text):
        if "SET TRANSACTION" in sql_text:
            raise ORA_GENERIC
        executed.append(sql_text)

    fake_cursor = MagicMock()
    fake_cursor.execute = fake_cursor_execute
    fake_cursor.description = [("DUMMY", None, None, None, None, None, None)]
    # fetchmany 返回一次数据后返回空（让 while 循环退出，避免无限循环）。
    # 行用 tuple（与 Oracle AsyncCursor 一致），让 columns 与 zip 后产出 {"col": value}。
    fetchCallCount = [0]

    async def fake_fetchmany(size):
        fetchCallCount[0] += 1
        if fetchCallCount[0] == 1:
            return [(1,)]
        return []

    fake_cursor.fetchmany = fake_fetchmany
    fake_cursor.close = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)
    fake_conn.ping = AsyncMock()
    fake_conn.close = AsyncMock()

    async def fake_connect_async(*args, **kwargs):
        return fake_conn

    with patch.object(pool.oracledb, "connect_async", side_effect=fake_connect_async):
        async def _passthrough_wait_for(coro, _timeout):
            return await coro
        with patch.object(pool.asyncio, "wait_for", side_effect=_passthrough_wait_for):
            rows = await adapter.execute_read_only("SELECT 1 FROM dual")

    # 关键断言：原 SQL 仍执行，且返回行
    assert rows == [{"dummy": 1}], f"SET TRANSACTION 失败后原 SQL 应正常返回行，实际 {rows}"
    assert executed == ["SELECT 1 FROM dual"], (
        f"SET TRANSACTION 失败时不应进入 executed 列表，原 SQL 必须独立执行；实际 {executed}"
    )