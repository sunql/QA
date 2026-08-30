"""business_db_pool 单元测试。

不依赖外部数据库：
- URL/DSN 构造与密码 URL 编码
- _assert_read_only 只读校验（白名单/黑名单/多语句/空）
- get_adapter 缓存与 dispose_adapter 释放
- build_adapter 按类型返回正确适配器
- DataSourceService.test_connection 使用 mock 适配器
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.enums import DataSourceType
from app.domain.exceptions import SqlSafetyError
from app.domain.models import DataSource
from app.infrastructure import business_db_pool as pool
from app.services.datasource_service import DataSourceService
from app.domain.schemas import DataSourceTestRequest


class TestAssertReadOnly:
    def test_accepts_select(self) -> None:
        pool._assert_read_only("SELECT 1")

    def test_accepts_with_cte(self) -> None:
        pool._assert_read_only("WITH cte AS (SELECT 1) SELECT * FROM cte")

    def test_accepts_select_with_semicolon_inside_string(self) -> None:
        pool._assert_read_only("SELECT * FROM t WHERE x = 'a;b'")

    def test_rejects_insert(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("INSERT INTO t VALUES (1)")

    def test_rejects_update(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("UPDATE t SET x = 1")

    def test_rejects_delete(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("DELETE FROM t")

    def test_rejects_drop(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("DROP TABLE t")

    def test_rejects_alter(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("ALTER TABLE t ADD COLUMN x INT")

    def test_rejects_grant(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("GRANT SELECT ON t TO u")

    def test_rejects_multi_statement(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT 1; DROP TABLE t")

    def test_rejects_empty(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("")

    def test_rejects_unknown_verb(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("EXPLAIN SELECT 1")

    # —— 深度扫描：首 token 合法但语义为写/侧信道的形态 ——

    def test_rejects_data_modifying_cte(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only(
                "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x"
            )

    def test_rejects_select_into(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT * INTO t2 FROM t")

    def test_rejects_select_into_outfile(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT * FROM t INTO OUTFILE '/tmp/dump'")

    def test_rejects_for_update(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT * FROM t FOR UPDATE")

    def test_rejects_for_share(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT * FROM t FOR SHARE")

    def test_rejects_nextval(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT nextval('s')")

    def test_rejects_pg_read_file(self) -> None:
        with pytest.raises(SqlSafetyError):
            pool._assert_read_only("SELECT pg_read_file('/etc/passwd')")

    def test_accepts_write_words_inside_string_literal(self) -> None:
        pool._assert_read_only("SELECT * FROM t WHERE x = 'DELETE FROM t'")

    def test_accepts_delete_as_column_name(self) -> None:
        pool._assert_read_only('SELECT "DELETE" AS col FROM t')


class TestBuildUrl:
    def test_postgres_url_encodes_password(self) -> None:
        url = pool._build_sqlalchemy_url(
            DataSourceType.POSTGRESQL, "h", 5432, "db", "u", "p@ss:1"
        )
        assert url == "postgresql+asyncpg://u:p%40ss%3A1@h:5432/db"

    def test_mysql_url_encodes_password(self) -> None:
        url = pool._build_sqlalchemy_url(
            DataSourceType.MYSQL, "h", 3306, "db", "u", "p@ss"
        )
        assert url == "mysql+aiomysql://u:p%40ss@h:3306/db"

    def test_oracle_dsn(self) -> None:
        assert pool._build_oracle_dsn("h", 1521, "X3") == "h:1521/X3"


class TestBuildAdapter:
    def test_oracle_returns_oracle_adapter(self) -> None:
        adapter = pool.build_adapter(
            DataSourceType.ORACLE, "h", 1521, "svc", "u", "p"
        )
        assert isinstance(adapter, pool._OracleAdapter)

    def test_postgres_returns_sqla_adapter(self) -> None:
        adapter = pool.build_adapter(
            DataSourceType.POSTGRESQL, "h", 5432, "db", "u", "p"
        )
        assert isinstance(adapter, pool._SqlaAdapter)


class _FakeOracleCursor:
    """模拟 oracledb AsyncCursor：execute/fetchmany 为协程，close 为同步方法。

    每次 fetchmany 都维护内部 offset：返回下一批（最多 limit 行），耗尽返回 []，
    模拟真实 oracledb 行为（fetchmany 不一次性返回全部结果集）。
    """

    def __init__(self, rows: list, description: list, *, streaming: bool = True) -> None:
        self._rows = rows
        self.description = description
        self.closed = False
        self._streaming = streaming
        self._offset = 0

    async def execute(self, sql: str) -> None:
        self.executedSql = sql
        self._offset = 0

    async def fetchmany(self, limit: int) -> list:
        if not self._streaming:
            # 老路径：保留原 `self._rows[:limit]` 语义（向后兼容老测试）
            return self._rows[:limit]
        batch = self._rows[self._offset : self._offset + limit]
        self._offset += len(batch)
        return batch

    def close(self) -> None:
        self.closed = True


class _FakeOracleConnection:
    def __init__(self, rows: list, description: list, *, streaming: bool = True) -> None:
        self._rows = rows
        self.description = description
        self._streaming = streaming
        self.closed = False

    def cursor(self) -> _FakeOracleCursor:
        return _FakeOracleCursor(self._rows, self.description, streaming=self._streaming)

    async def close(self) -> None:
        self.closed = True


class TestOracleAdapterExecute:
    async def test_execute_read_only_returns_mapped_rows(self, monkeypatch) -> None:
        # Arrange: 用假连接替换 oracledb.connect_async，覆盖 _run 的 cursor 关闭路径
        conn = _FakeOracleConnection([("A", 1), ("B", 2)], [("NAME",), ("QTY",)])
        captured: dict[str, object] = {}

        async def _fakeConnectAsync(**kwargs):
            captured.update(kwargs)
            return conn

        monkeypatch.setattr(pool.oracledb, "connect_async", _fakeConnectAsync)
        adapter = pool._OracleAdapter("h", 1521, "svc", "u", "p")
        # Act
        rows = await adapter.execute_read_only("SELECT NAME, QTY FROM T")
        # Assert
        assert rows == [{"NAME": "A", "QTY": 1}, {"NAME": "B", "QTY": 2}]
        assert captured["user"] == "u"
        assert captured["dsn"] == "h:1521/svc"
        # 同步 cursor.close() 被调用、异步 conn.close() 被 await
        assert conn.closed is True

    async def test_execute_read_only_rejects_write(self, monkeypatch) -> None:
        adapter = pool._OracleAdapter("h", 1521, "svc", "u", "p")
        with pytest.raises(SqlSafetyError):
            await adapter.execute_read_only("DELETE FROM T")

    async def test_execute_read_only_quotes_digit_leading_alias(self, monkeypatch) -> None:
        """集成：执行前规整数字开头别名（AS 定义处 + t. 引用处），消除 ORA-00923。"""
        conn = _RecordingOracleConnection([(1,)], [("N",)])

        async def _fakeConnectAsync(**kwargs):
            return conn

        monkeypatch.setattr(pool.oracledb, "connect_async", _fakeConnectAsync)
        adapter = pool._OracleAdapter("h", 1521, "svc", "u", "p")
        sql = "SELECT t.2025采购量 FROM (SELECT SUM(QTY) AS 2025采购量 FROM T) t"
        await adapter.execute_read_only(sql)
        assert conn.executed == ['SELECT t."2025采购量" FROM (SELECT SUM(QTY) AS "2025采购量" FROM T) t']

    async def test_execute_read_only_injects_nulls_last(self, monkeypatch) -> None:
        """集成：裸 ORDER BY ... DESC 在执行前被补 NULLS LAST，消除跨年 top-N 抓 NULL 行。"""
        conn = _RecordingOracleConnection([(1,)], [("N",)])

        async def _fakeConnectAsync(**kwargs):
            return conn

        monkeypatch.setattr(pool.oracledb, "connect_async", _fakeConnectAsync)
        adapter = pool._OracleAdapter("h", 1521, "svc", "u", "p")
        sql = (
            "SELECT * FROM (SELECT ITMREF_0, SUM(QTYUOM_0) AS TOTAL_QTY_2025 "
            "FROM ZJTH.PORDERQ GROUP BY ITMREF_0 ORDER BY TOTAL_QTY_2025 DESC) "
            "WHERE ROWNUM <= 10"
        )
        await adapter.execute_read_only(sql)
        assert conn.executed == [
            "SELECT * FROM (SELECT ITMREF_0, SUM(QTYUOM_0) AS TOTAL_QTY_2025 "
            "FROM ZJTH.PORDERQ GROUP BY ITMREF_0 ORDER BY TOTAL_QTY_2025 DESC NULLS LAST) "
            "WHERE ROWNUM <= 10"
        ]

    async def test_execute_read_only_no_limit_drains_full_streaming_result(self, monkeypatch) -> None:
        """回归：queryRowLimit<=0 时必须循环 fetchmany 直到耗尽，不能被 cursor.arraysize=100 静默截断。

        真实 oracledb AsyncCursor.fetchmany(None) 默认按 cursor.arraysize=100 截行。
        用 streaming fake cursor（每次 fetchmany 仅返回下一批）验证适配器走的是循环路径。
        2500 行 = 3 批（1000+1000+500），远超 arraysize=100。
        """
        # Arrange: 2500 行 streaming；每批最多 1000 行
        allRows = [(f"K{i}", i) for i in range(2500)]
        conn = _FakeOracleConnection(allRows, [("K",), ("V",)], streaming=True)
        fakeSettings = SimpleNamespace(queryRowLimit=0, queryTimeoutSeconds=30)

        async def _fakeConnectAsync(**kwargs):
            return conn

        monkeypatch.setattr(pool.oracledb, "connect_async", _fakeConnectAsync)
        monkeypatch.setattr(pool, "getSettings", lambda: fakeSettings)
        adapter = pool._OracleAdapter("h", 1521, "svc", "u", "p")
        # Act
        rows = await adapter.execute_read_only("SELECT K, V FROM T")
        # Assert
        assert len(rows) == 2500
        assert rows[0] == {"K": "K0", "V": 0}
        assert rows[-1] == {"K": "K2499", "V": 2499}
        assert conn.closed is True

    async def test_execute_read_only_positive_limit_uses_single_fetchmany(self, monkeypatch) -> None:
        """queryRowLimit>0 时仍是单次 fetchmany(limit)，不会进入循环路径。"""
        allRows = [(i, i * 10) for i in range(50)]
        fetchCalls: list[int] = []

        class _SpyCursor(_FakeOracleCursor):
            async def fetchmany(self, limit: int) -> list:
                fetchCalls.append(limit)
                return await super().fetchmany(limit)

        class _SpyConnection(_FakeOracleConnection):
            def cursor(self) -> _FakeOracleCursor:  # type: ignore[override]
                return _SpyCursor(allRows, [("N",), ("V",)], streaming=True)

        conn = _SpyConnection(allRows, [("N",), ("V",)], streaming=True)
        fakeSettings = SimpleNamespace(queryRowLimit=10, queryTimeoutSeconds=30)

        async def _fakeConnectAsync(**kwargs):
            return conn

        monkeypatch.setattr(pool.oracledb, "connect_async", _fakeConnectAsync)
        monkeypatch.setattr(pool, "getSettings", lambda: fakeSettings)
        adapter = pool._OracleAdapter("h", 1521, "svc", "u", "p")
        rows = await adapter.execute_read_only("SELECT N, V FROM T")
        assert len(rows) == 10
        # 单次 fetchmany(10)，不会循环
        assert fetchCalls == [10]


class _RecordingCursor(_FakeOracleCursor):
    def __init__(self, rows, description, sink: list) -> None:
        super().__init__(rows, description)
        self._sink = sink

    async def execute(self, sql: str) -> None:
        self.executedSql = sql
        self._sink.append(sql)


class _RecordingOracleConnection(_FakeOracleConnection):
    """录制 cursor 实际收到的 SQL，验证执行前规整生效。"""

    def __init__(self, rows, description) -> None:
        super().__init__(rows, description)
        self.executed: list[str] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self._rows, self.description, self.executed)


class TestQuoteDigitLeadingIdentifiers:
    """执行前规整：给数字开头别名加双引号，消除 ORA-00923。"""

    def test_quotes_digit_leading_alias_after_as(self) -> None:
        assert (
            pool._quote_digit_leading_identifiers("SELECT SUM(QTY) AS 2025采购量 FROM T")
            == 'SELECT SUM(QTY) AS "2025采购量" FROM T'
        )

    def test_quotes_digit_leading_alias_after_dot(self) -> None:
        assert (
            pool._quote_digit_leading_identifiers("SELECT t.2025采购量 FROM T t")
            == 'SELECT t."2025采购量" FROM T t'
        )

    def test_quotes_multiple_occurrences(self) -> None:
        sql = (
            "SELECT t.2025采购量, t.2025平均价格, (t.2026平均价格 - t.2025平均价格) AS 价格差异 "
            "FROM (SELECT SUM(QTYUOM_0) AS 2025采购量 FROM ZJTH.PORDERQ d) t"
        )
        out = pool._quote_digit_leading_identifiers(sql)
        assert 't."2025采购量"' in out
        assert 't."2025平均价格"' in out
        assert 't."2026平均价格"' in out
        assert 'AS "2025采购量"' in out
        assert 'AS 价格差异' in out  # 中文开头别名不受影响

    def test_does_not_quote_pure_number_literal(self) -> None:
        sql = "SELECT * FROM ZJTH.PORDER WHERE EXTRACT(YEAR FROM ORDDAT_0) = 2025"
        assert pool._quote_digit_leading_identifiers(sql) == sql

    def test_does_not_quote_rownum_number(self) -> None:
        sql = "SELECT * FROM (SELECT t.* FROM T t) WHERE ROWNUM <= 10"
        assert pool._quote_digit_leading_identifiers(sql) == sql

    def test_does_not_quote_letter_leading_identifier(self) -> None:
        sql = "SELECT AVG_PRICE_2025, CPRPRI_0, d.ORDDAT_0 FROM T d"
        assert pool._quote_digit_leading_identifiers(sql) == sql

    def test_does_not_double_quote_already_quoted(self) -> None:
        sql = 'SELECT SUM(QTY) AS "2025采购量" FROM T'
        assert pool._quote_digit_leading_identifiers(sql) == sql

    def test_does_not_touch_string_literal(self) -> None:
        """字符串字面量内的数字文本不得被误改（含 .数字中文 形态）。"""
        sql = "SELECT * FROM T WHERE REM = '2025年.2026年' AND X = '2025-01-01'"
        assert pool._quote_digit_leading_identifiers(sql) == sql

    def test_empty_sql_returns_empty(self) -> None:
        assert pool._quote_digit_leading_identifiers("") == ""


class TestInjectNullsLast:
    """执行前兜底：给裸 `ORDER BY ... DESC` 自动补 NULLS LAST（Oracle DESC 默认 NULLS FIRST）。"""

    def test_appends_nulls_last_to_bare_desc(self) -> None:
        sql = "SELECT * FROM (SELECT SUM(QTY) AS TOTAL_QTY_2025 FROM T GROUP BY M) ORDER BY TOTAL_QTY_2025 DESC"
        assert pool._inject_nulls_last(sql).endswith(
            "ORDER BY TOTAL_QTY_2025 DESC NULLS LAST"
        )

    def test_injects_into_order_by_inside_inline_view(self) -> None:
        sql = (
            "SELECT * FROM (SELECT ITMREF_0, SUM(QTYUOM_0) AS TOTAL_QTY_2025 "
            "FROM ZJTH.PORDERQ GROUP BY ITMREF_0 ORDER BY TOTAL_QTY_2025 DESC) "
            "WHERE ROWNUM <= 10"
        )
        out = pool._inject_nulls_last(sql)
        assert "ORDER BY TOTAL_QTY_2025 DESC NULLS LAST) WHERE ROWNUM" in out

    def test_respects_explicit_nulls_clause(self) -> None:
        sql = "SELECT * FROM T ORDER BY QTY DESC NULLS FIRST"
        assert pool._inject_nulls_last(sql) == sql

    def test_handles_multi_term_order_by(self) -> None:
        sql = "SELECT * FROM T ORDER BY A DESC, B ASC"
        assert pool._inject_nulls_last(sql) == "SELECT * FROM T ORDER BY A DESC NULLS LAST, B ASC"

    def test_does_not_touch_asc_only(self) -> None:
        sql = "SELECT * FROM T ORDER BY QTY ASC"
        assert pool._inject_nulls_last(sql) == sql

    def test_no_order_by_no_change(self) -> None:
        sql = "SELECT ITMREF_0, SUM(QTYUOM_0) FROM ZJTH.PORDERQ GROUP BY ITMREF_0"
        assert pool._inject_nulls_last(sql) == sql

    def test_does_not_touch_string_literal_desc(self) -> None:
        sql = "SELECT * FROM T WHERE REM = 'order desc' ORDER BY QTY DESC"
        assert pool._inject_nulls_last(sql) == (
            "SELECT * FROM T WHERE REM = 'order desc' ORDER BY QTY DESC NULLS LAST"
        )

    def test_does_not_touch_quoted_identifier_desc(self) -> None:
        sql = 'SELECT "DESC", QTY FROM T ORDER BY QTY DESC'
        assert pool._inject_nulls_last(sql) == (
            'SELECT "DESC", QTY FROM T ORDER BY QTY DESC NULLS LAST'
        )

    def test_does_not_double_append(self) -> None:
        sql = "SELECT * FROM T ORDER BY QTY DESC NULLS LAST"
        assert pool._inject_nulls_last(sql) == sql

    def test_empty_sql_returns_empty(self) -> None:
        assert pool._inject_nulls_last("") == ""


class TestAdapterCache:
    async def test_get_adapter_caches_instance(self, monkeypatch) -> None:
        # Arrange: 跳过真实解密
        monkeypatch.setattr(pool, "decryptApiKey", lambda cipher: "secret")
        await pool.reset_pool()
        ds = DataSource(
            id=1,
            name="ds",
            type=DataSourceType.POSTGRESQL,
            host="h",
            port=5432,
            database_name="db",
            username="u",
            password_encrypted="cipher",
        )
        # Act
        first = pool.get_adapter(1, ds)
        second = pool.get_adapter(1, ds)
        # Assert
        assert first is second

    async def test_dispose_adapter_removes_cache(self, monkeypatch) -> None:
        monkeypatch.setattr(pool, "decryptApiKey", lambda cipher: "secret")
        await pool.reset_pool()
        ds = DataSource(
            id=2,
            name="ds2",
            type=DataSourceType.POSTGRESQL,
            host="h",
            port=5432,
            database_name="db",
            username="u",
            password_encrypted="cipher",
        )
        first = pool.get_adapter(2, ds)
        await pool.dispose_adapter(2)
        second = pool.get_adapter(2, ds)
        # Assert: 释放后再次获取应得到新实例
        assert first is not second
        await pool.dispose_adapter(2)


class _FakeAdapter:
    def __init__(self, success: bool, message: str) -> None:
        self._success = success
        self._message = message
        self.disposed = False

    async def test(self) -> tuple[bool, str]:
        return self._success, self._message

    async def dispose(self) -> None:
        self.disposed = True


class TestServiceTestConnection:
    async def test_success_returns_response(self, monkeypatch) -> None:
        fake = _FakeAdapter(True, "连接成功")

        def _build(*args, **kwargs):
            return fake

        monkeypatch.setattr("app.services.datasource_service.build_adapter", _build)
        dto = DataSourceTestRequest(
            type=DataSourceType.POSTGRESQL,
            host="h",
            port=5432,
            database_name="db",
            username="u",
            password="p",
        )
        result = await DataSourceService().test_connection(dto)
        assert result.success is True
        assert "连接成功" in result.message
        assert fake.disposed is True

    async def test_failure_returns_response(self, monkeypatch) -> None:
        fake = _FakeAdapter(False, "connection refused")

        def _build(*args, **kwargs):
            return fake

        monkeypatch.setattr("app.services.datasource_service.build_adapter", _build)
        dto = DataSourceTestRequest(
            type=DataSourceType.ORACLE,
            host="h",
            port=1521,
            database_name="svc",
            username="u",
            password="p",
        )
        result = await DataSourceService().test_connection(dto)
        assert result.success is False
        assert "connection refused" in result.message


class _FakeMappings:
    """模拟 SQLAlchemy MappingResult；fetchmany(None) 复刻 aiomysql 行为（arraysize 默认 1 行）。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.allCalled = False
        self.fetchManySize: int | None = None

    def all(self) -> list[dict]:
        self.allCalled = True
        return list(self._rows)

    def fetchmany(self, size: int | None = None) -> list[dict]:
        self.fetchManySize = size
        if size is None:
            return list(self._rows[:1])  # aiomysql: fetchmany(None) 按 arraysize=1 取行
        return list(self._rows[:size])


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._mappings = _FakeMappings(rows)

    def mappings(self) -> _FakeMappings:
        return self._mappings


class _FakeConn:
    def __init__(self, result: _FakeResult) -> None:
        self._result = result

    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str) -> _FakeResult:
        return self._result


class _FakeEngine:
    """模拟 SQLAlchemy AsyncEngine：connect() 同步返回 AsyncConnection（而非协程）。"""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self) -> _FakeConn:
        return self._conn


class TestSqlaAdapterRowFetch:
    """回归：queryRowLimit<=0（无限）时必须显式 all() 取全部行。

    背景：aiomysql 的 fetchmany(None) 按 cursor.arraysize（默认 1）只返回 1 行，
    “fetchmany(None)=全部行”的假设仅对 asyncpg 成立。旧实现导致 MySQL 数据源
    introspection 每个查询只取到首行（表/列/外键大面积丢失）。
    """

    async def test_unlimited_limit_returns_all_rows(self, monkeypatch) -> None:
        rows = [{"a": 1}, {"a": 2}, {"a": 3}]
        engine = _FakeEngine(_FakeConn(_FakeResult(rows)))
        monkeypatch.setattr(pool, "create_async_engine", lambda url, **kwargs: engine)
        monkeypatch.setattr(
            pool, "getSettings", lambda: SimpleNamespace(queryRowLimit=0, queryTimeoutSeconds=30)
        )
        adapter = pool._SqlaAdapter("mysql+aiomysql://u:p@h:3306/db")
        result = await adapter.execute_read_only("SELECT a FROM t")
        assert result == rows
        assert engine._conn._result._mappings.allCalled is True

    async def test_positive_limit_uses_fetchmany(self, monkeypatch) -> None:
        rows = [{"a": 1}, {"a": 2}, {"a": 3}]
        engine = _FakeEngine(_FakeConn(_FakeResult(rows)))
        monkeypatch.setattr(pool, "create_async_engine", lambda url, **kwargs: engine)
        monkeypatch.setattr(
            pool, "getSettings", lambda: SimpleNamespace(queryRowLimit=2, queryTimeoutSeconds=30)
        )
        adapter = pool._SqlaAdapter("mysql+aiomysql://u:p@h:3306/db")
        result = await adapter.execute_read_only("SELECT a FROM t")
        assert result == [{"a": 1}, {"a": 2}]
        assert engine._conn._result._mappings.fetchManySize == 2
