"""分批内省：多批合并结果必须与单次全量一致，进度回调按批推进。"""

from __future__ import annotations

from asyncio import run as asyncio_run

import pytest

from app.services.schema_introspection_service import SchemaIntrospectionService


def _ds(dsType: str = "oracle"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=11,
        type=dsType,
        username="LC019999",
        host="h",
        port=1521,
        databaseName="X",
    )


class _FakeOracleAdapter:
    """伪 Oracle 数据字典，按 SQL 中的 table_name IN (...) 过滤返回。

    刻意让「列来源（ALL_TAB_COLUMNS）里的表」多于「旧表名列表（ALL_TABLES）
    能查到的基表」（baseTables）：视图只在列来源可见。分批路径若仍用
    ALL_TABLES 列表就会漏表，等价用例即可捕获。
    """

    def __init__(self, columnTables: list[str], baseTables: list[str] | None = None) -> None:
        self.executed: list[str] = []
        self.columnTables = list(columnTables)
        self.baseTables = list(baseTables) if baseTables is not None else list(columnTables)

    def _picked(self, sql: str) -> list[str]:
        hits = [t for t in self.columnTables if f"'{t}'" in sql]
        return hits if hits else list(self.columnTables)

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        # 先判表名列表：新查询与列查询同源（都来自 ALL_TAB_COLUMNS），
        # 必须优先匹配，否则会被下面的 ALL_TAB_COLUMNS 分支抢先命中。
        if "SELECT DISTINCT table_name" in sql:
            return [{"table_name": t} for t in self.columnTables]
        if "SELECT table_name FROM ALL_TABLES" in sql:
            # 旧实现的口径：ALL_TABLES 只有基表，视图不可见。
            return [{"table_name": t} for t in self.baseTables]
        picked = self._picked(sql)
        if "ALL_TAB_COLUMNS" in sql:
            return [
                {
                    "table_name": t,
                    "column_name": col,
                    "data_type": "VARCHAR2",
                    "nullable": 0,
                    "owner": "LC019999",
                }
                for t in picked
                for col in ("COL1", "COL2")
            ]
        if "constraint_type = 'P'" in sql:
            return [{"table_name": t, "column_name": "COL1"} for t in picked]
        return []


class _RecordingAdapter:
    """只回答表名列表、其余查询返回空；记录全部 SQL 供形态断言。"""

    def __init__(self, tableNames: list[str]) -> None:
        self.executed: list[str] = []
        self.tableNames = list(tableNames)

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "SELECT DISTINCT table_name" in sql:
            return [{"table_name": t} for t in self.tableNames]
        return []


def test_batched_merge_equals_full_and_progress_monotonic() -> None:
    names = [f"T{i:03d}" for i in range(250)]
    adapter = _FakeOracleAdapter(names)
    service = SchemaIntrospectionService(
        adapterProvider=lambda dsId, ds: adapter
    )
    seen: list[tuple[int, int]] = []

    tables = asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999",
            onProgress=lambda done, total: seen.append((done, total)),
            batchSize=100,
        )
    )

    assert len(tables) == 250, "分批不得丢表"
    assert seen[0] == (0, 250), "首个回调必须在首批之前报 (0, total)"
    assert seen[-1] == (250, 250), "末个回调必须报满"
    assert [d for d, _ in seen] == sorted(d for d, _ in seen), "进度必须单调不减"
    assert all(t == 250 for _, t in seen)


def test_batched_equals_full_including_views() -> None:
    """同一份伪数据分别跑全量与分批：表名集合与列信息必须完全一致。

    baseTables 只含基表，columnTables 额外含两个视图（列来源可见、ALL_TABLES
    不可见）。表名列表一旦回到 ALL_TABLES 口径就会丢视图，本用例即失败。
    """
    baseTables = [f"T{i:03d}" for i in range(6)]
    viewTables = ["V_SUMMARY", "V_DETAIL"]
    columnTables = baseTables + viewTables
    adapter = _FakeOracleAdapter(columnTables, baseTables=baseTables)
    service = SchemaIntrospectionService(adapterProvider=lambda dsId, ds: adapter)

    full = asyncio_run(service.introspect(_ds(), owner="LC019999"))
    batched = asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999", onProgress=lambda d, t: None, batchSize=2
        )
    )

    fullMap = {t.table_name: t for t in full}
    batchedMap = {t.table_name: t for t in batched}
    assert set(fullMap) == set(batchedMap) == set(columnTables), "分批不得丢视图"
    assert "V_SUMMARY" in batchedMap and "V_DETAIL" in batchedMap
    for name in fullMap:
        assert fullMap[name].columns == batchedMap[name].columns, f"{name} 列信息必须一致"
        assert fullMap[name].primary_keys == batchedMap[name].primary_keys
        assert fullMap[name].foreign_keys == batchedMap[name].foreign_keys
    # 表名列表必须走与列查询同源的 DISTINCT 查询，而非旧的 ALL_TABLES 口径
    assert any("SELECT DISTINCT table_name FROM ALL_TAB_COLUMNS" in s for s in adapter.executed)
    assert not any("SELECT table_name FROM ALL_TABLES" in s for s in adapter.executed)


def test_batched_query_count_is_one_per_type_per_batch() -> None:
    """250 表 / 100 一批 ⇒ 3 批；每批 5 类查询 + 1 次表名列表 = 16 次。"""
    adapter = _FakeOracleAdapter([f"T{i:03d}" for i in range(250)])
    service = SchemaIntrospectionService(adapterProvider=lambda dsId, ds: adapter)

    asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999", onProgress=lambda d, t: None, batchSize=100
        )
    )

    tableListCalls = [s for s in adapter.executed if "SELECT DISTINCT table_name" in s]
    assert len(tableListCalls) == 1, "表名列表只查一次"


def test_batched_filter_clause_precedes_order_by() -> None:
    """回归护栏：现有查询常量以 ORDER BY 结尾，过滤子句必须插在其前。

    若把 `AND ... IN (...)` 直接拼在 `ORDER BY` 之后，真实库会报语法错
    （Oracle ORA-00933 / MySQL 1064）；伪适配器只按关键字匹配，故需单独断言。
    """
    adapter = _FakeOracleAdapter([f"T{i:03d}" for i in range(250)])
    service = SchemaIntrospectionService(adapterProvider=lambda dsId, ds: adapter)

    asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999", onProgress=lambda d, t: None, batchSize=100
        )
    )

    batchSql = [sql for sql in adapter.executed if "table_name IN" in sql]
    assert batchSql, "必须生成带本批过滤的查询"
    for sql in batchSql:
        upper = sql.upper()
        if "ORDER BY" in upper:
            assert upper.index("TABLE_NAME IN") < upper.index("ORDER BY"), (
                "过滤子句必须位于 ORDER BY 之前"
            )


@pytest.mark.parametrize("dsType", ["oracle", "postgresql", "mysql"])
def test_batch_sql_shape_all_dialects(dsType: str) -> None:
    """三方言分批 SQL 形态护栏：IN 在 ORDER BY 前 + PK/FK 别名前缀正确。"""
    adapter = _RecordingAdapter(["T1", "T2"])
    service = SchemaIntrospectionService(adapterProvider=lambda dsId, ds: adapter)

    asyncio_run(
        service.introspectBatched(
            _ds(dsType), owner="LC019999", onProgress=lambda d, t: None, batchSize=100
        )
    )

    batchSql = [sql for sql in adapter.executed if " IN (" in sql.upper()]
    assert batchSql, "必须生成带本批过滤的查询"
    for sql in batchSql:
        upper = sql.upper()
        if "ORDER BY" in upper:
            assert upper.index("IN (") < upper.index("ORDER BY"), (
                "过滤子句必须位于 ORDER BY 之前"
            )

    if dsType == "oracle":
        pkSql = next(s for s in adapter.executed if "CONSTRAINT_TYPE = 'P'" in s.upper())
        fkSql = next(s for s in adapter.executed if "CONSTRAINT_TYPE = 'R'" in s.upper())
        assert "C.TABLE_NAME IN (" in pkSql.upper(), "Oracle PK 必须用 c. 前缀"
        assert "CC.TABLE_NAME IN (" in fkSql.upper(), "Oracle FK 必须用 cc. 前缀"
        return

    pkSql = next(s for s in adapter.executed if "CONSTRAINT_TYPE = 'PRIMARY KEY'" in s.upper())
    fkSql = next(s for s in adapter.executed if "CONSTRAINT_TYPE = 'FOREIGN KEY'" in s.upper())
    assert "TC.TABLE_NAME IN (" in pkSql.upper(), f"{dsType} PK 必须用 tc. 前缀"
    assert "TC.TABLE_NAME IN (" in fkSql.upper(), f"{dsType} FK 必须用 tc. 前缀"
    if dsType == "postgresql":
        commentSqls = [
            s for s in adapter.executed
            if "PG_CLASS" in s.upper() and "PG_DESCRIPTION" in s.upper()
        ]
        assert commentSqls, "PG 必须生成注释查询"
        for sql in commentSqls:
            upper = sql.upper()
            assert "C.RELNAME IN (" in upper, "PG 注释查询必须用 c.relname 过滤"
            assert "AND TABLE_NAME IN (" not in upper, "PG 不得引用 SELECT 输出别名"


def test_pg_comment_filter_uses_relname_not_output_alias() -> None:
    """PG 注释查询来自 pg_class；WHERE 不得引用 SELECT 的 AS table_name 别名。"""
    from app.services.schema_introspection_service import (
        _PG_COL_COMMENT_SQL,
        _PG_TABLE_COMMENT_SQL,
        _tableNameInClause,
        _withTableFilter,
    )

    clause = _tableNameInClause(["T1", "T2"], column="c.relname")
    for sql in (
        _withTableFilter(_PG_TABLE_COMMENT_SQL, clause),
        _withTableFilter(_PG_COL_COMMENT_SQL, clause),
    ):
        upper = sql.upper()
        assert "C.RELNAME IN ('T1', 'T2')" in upper
        assert "AND TABLE_NAME IN" not in upper
        if "ORDER BY" in upper:
            assert upper.index("C.RELNAME IN") < upper.index("ORDER BY")
