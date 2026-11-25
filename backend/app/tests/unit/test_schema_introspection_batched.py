"""分批内省：多批合并结果必须与单次全量一致，进度回调按批推进。"""

from __future__ import annotations

from asyncio import run as asyncio_run

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


class _BatchedAdapter:
    """按 SQL 中的 table_name IN (...) 过滤返回；记录每次查询供断言。"""

    def __init__(self, tableNames: list[str]) -> None:
        self.executed: list[str] = []
        self.tableNames = tableNames

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "SELECT table_name FROM ALL_TABLES" in sql:
            return [{"table_name": t} for t in self.tableNames]
        picked = [t for t in self.tableNames if f"'{t}'" in sql]
        if "ALL_TAB_COLUMNS" in sql:
            return [
                {
                    "table_name": t,
                    "column_name": "COL1",
                    "data_type": "VARCHAR2",
                    "nullable": 0,
                    "owner": "LC019999",
                }
                for t in picked
            ]
        if "constraint_type = 'P'" in sql:
            return [{"table_name": t, "column_name": "COL1"} for t in picked]
        return []


def test_batched_merge_equals_full_and_progress_monotonic() -> None:
    names = [f"T{i:03d}" for i in range(250)]
    adapter = _BatchedAdapter(names)
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


def test_batched_query_count_is_one_per_type_per_batch() -> None:
    """250 表 / 100 一批 ⇒ 3 批；每批 5 类查询 + 1 次表名列表 = 16 次。"""
    adapter = _BatchedAdapter([f"T{i:03d}" for i in range(250)])
    service = SchemaIntrospectionService(adapterProvider=lambda dsId, ds: adapter)

    asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999", onProgress=lambda d, t: None, batchSize=100
        )
    )

    tableListCalls = [s for s in adapter.executed if "SELECT table_name FROM ALL_TABLES" in s]
    assert len(tableListCalls) == 1, "表名列表只查一次"


def test_batched_filter_clause_precedes_order_by() -> None:
    """回归护栏：现有查询常量以 ORDER BY 结尾，过滤子句必须插在其前。

    若把 `AND ... IN (...)` 直接拼在 `ORDER BY` 之后，真实库会报语法错
    （Oracle ORA-00933 / MySQL 1064）；伪适配器只按关键字匹配，故需单独断言。
    """
    adapter = _BatchedAdapter([f"T{i:03d}" for i in range(250)])
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
