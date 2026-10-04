"""ResearchSqlRunner：只读执行 + 行上限/超时 + 验证失败不抛（真实 PG + 真实适配器）。

Task 13e 契约变更：`executeReadonlySql` / `runVerification` 的**业务 SQL 一律经调用方
按次送达的业务库 adapter 执行**（`adapter=`），不再打在注入的元数据库 session 上。
本文件的 `test_execute_readonly_never_uses_metadata_session` 是这条根因的**回归守卫**：
此前没有任何断言盯着「SQL 打在哪个库上」，所以两次真机验收（13b 6/6、13d `.sql` 事件）
都从未触达业务数据却全绿。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.engine import make_url

from app.domain.enums import DataSourceType
from app.infrastructure import business_db_pool
from app.infrastructure.business_db_pool import build_adapter
from app.services.hypothesis_service import Hypothesis
from app.services.research_sql_runner import VERIFICATION_ROW_CAP, ResearchSqlRunner
from app.tests._pg_support import resolveTestDatabaseUrl


class SpyAdapter:
    """假业务库适配器：记录收到的 SQL，返回固定行（断言「SQL 真的到了 adapter」）。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[str] = []
        self._rows = rows if rows is not None else [{"ok": 1}]

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.calls.append(sql)
        return list(self._rows)


class _TinyTimeoutSettings:
    """把适配器侧超时压到 50ms（真实取值来自 `QUERY_TIMEOUT_SECONDS`，默认 30s）。"""

    queryRowLimit = 0
    queryTimeoutSeconds = 0.05


@pytest.fixture()
async def businessAdapter() -> AsyncIterator[Any]:
    """**真实**业务库适配器。

    指向测试库：本机没有第二个真实业务库，而本文件要验的是「业务 SQL 经
    `execute_read_only` 路径执行（独立连接 + 库侧只读 + 限行）」这一事实，不是某个
    特定引擎的方言。刻意用 `build_adapter` 而非 `get_adapter`：前者不写进程级缓存
    （缓存适配器会跨测试的事件循环复用连接池）。
    """
    url = make_url(resolveTestDatabaseUrl())
    adapter = build_adapter(
        DataSourceType.POSTGRESQL, url.host, url.port, url.database, url.username, url.password
    )
    yield adapter
    await adapter.dispose()


@pytest.mark.asyncio
async def test_execute_readonly_select(dbSession, businessAdapter) -> None:
    runner = ResearchSqlRunner()
    rows = await runner.executeReadonlySql(
        dbSession, "SELECT 1 AS ONE FROM (SELECT 1) T", adapter=businessAdapter
    )
    assert rows and list(rows[0].values())[0] == 1


@pytest.mark.asyncio
async def test_rejects_dml(dbSession, businessAdapter) -> None:
    runner = ResearchSqlRunner()
    with pytest.raises(ValueError):
        await runner.executeReadonlySql(
            dbSession, "DELETE FROM research_session", adapter=businessAdapter
        )


@pytest.mark.asyncio
async def test_execute_readonly_never_uses_metadata_session() -> None:
    """回归守卫（Task 13e 根因）：业务 SQL 经 adapter 执行，元数据库会话**一行都不执行**。

    历史缺陷是「SQL 打在调用方注入的元数据库会话上」——业务表在 Oracle，元数据库是
    Postgres ⇒ 必然 `UndefinedTableError`。这条断言把「执行面到底是哪个连接」钉死。
    """
    spySession = AsyncMock()
    spyAdapter = SpyAdapter(rows=[{"answer": 42}])
    rows = await ResearchSqlRunner().executeReadonlySql(
        spySession, "SELECT 42 AS ANSWER", adapter=spyAdapter
    )
    assert rows == [{"answer": 42}]  # 结果来自 adapter
    assert spyAdapter.calls == ["SELECT 42 AS ANSWER"]  # SQL 确实交给了 adapter
    spySession.execute.assert_not_called()  # 元数据库会话未被用来执行业务 SQL


@pytest.mark.asyncio
async def test_execute_readonly_rejects_missing_adapter() -> None:
    """adapter 未送达 ⇒ 显式 RuntimeError，**绝不**静默回落到元数据库会话。"""
    with pytest.raises(RuntimeError):
        await ResearchSqlRunner().executeReadonlySql(
            AsyncMock(), "SELECT 1 AS ONE", adapter=None
        )


@pytest.mark.asyncio
async def test_run_verification_uses_adapter() -> None:
    """假设验证 SQL 与执行步**同一条业务库路径**（O2 引用的不存在小写表即此根因）。"""
    spySession = AsyncMock()
    spyAdapter = SpyAdapter(rows=[{"cnt": 7}])
    good = Hypothesis(statement="s", driver=None, verificationSql="SELECT 7 AS cnt")
    outcome = await ResearchSqlRunner().runVerification(spySession, good, adapter=spyAdapter)
    assert outcome == {"rows": [{"cnt": 7}], "error": None}
    assert spyAdapter.calls == ["SELECT 7 AS cnt"]
    spySession.execute.assert_not_called()


@pytest.mark.asyncio
async def test_run_verification_returns_error_dict_not_raise(dbSession, businessAdapter) -> None:
    runner = ResearchSqlRunner()
    bad = Hypothesis(statement="s", driver=None, verificationSql="SELECT * FROM 不存在的表")
    outcome = await runner.runVerification(dbSession, bad, adapter=businessAdapter)
    assert outcome["error"] is not None and outcome["rows"] == []


@pytest.mark.asyncio
async def test_run_verification_failure_leaves_session_usable(dbSession, businessAdapter) -> None:
    """失败后会话仍可用（失败路径内建 rollback；Task 13e 后业务 SQL 已不污染 session）。"""
    runner = ResearchSqlRunner()
    bad = Hypothesis(statement="s", driver=None, verificationSql="SELECT * FROM 不存在的表")
    assert (await runner.runVerification(dbSession, bad, adapter=businessAdapter))["error"] is not None
    rows = await runner.executeReadonlySql(
        dbSession, "SELECT 1 AS ONE FROM (SELECT 1) T", adapter=businessAdapter
    )
    assert rows and list(rows[0].values())[0] == 1


@pytest.mark.asyncio
async def test_execute_readonly_truncates_over_row_cap(dbSession, businessAdapter) -> None:
    """超过行上限 → 截断到 VERIFICATION_ROW_CAP。

    适配器侧的限行来自 `QUERY_ROW_LIMIT`（本环境默认 0 = 不限行）⇒ 本层截断是研究侧
    对「验证查询结果形状」的独立保证，与配置是否限行解耦。
    """
    runner = ResearchSqlRunner()
    sql = f"SELECT n FROM generate_series(1, {VERIFICATION_ROW_CAP + 5}) AS n"
    rows = await runner.executeReadonlySql(dbSession, sql, adapter=businessAdapter)
    assert len(rows) == VERIFICATION_ROW_CAP


@pytest.mark.asyncio
async def test_execute_readonly_times_out(
    dbSession, businessAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时 → TimeoutError（闸门由适配器路径提供：`QUERY_TIMEOUT_SECONDS`）。

    pg_sleep 被 SQL Guard 的 _FORBIDDEN_CALLS 拒绝（时序侧信道，见 business_db_pool.py），
    故用 generate_series 造一条必超时的重查询；超时配置压到 50ms，不真等满默认 30s。
    """
    monkeypatch.setattr(business_db_pool, "getSettings", lambda: _TinyTimeoutSettings())
    runner = ResearchSqlRunner()
    with pytest.raises(TimeoutError):
        await runner.executeReadonlySql(
            dbSession,
            "SELECT COUNT(*) FROM generate_series(1, 50000000) AS n",
            adapter=businessAdapter,
        )
