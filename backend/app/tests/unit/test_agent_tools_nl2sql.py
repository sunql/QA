"""NL2SQL 工具 handler 测试（TDD: 先写失败测试）。

测试 5 个 handler + dispatcher，使用 fake AsyncSession。
所有触 DB handler 均走 fake session，不依赖真实 PG。
"""

from __future__ import annotations

import json
import pytest
import pytest_asyncio

from app.infrastructure.llm.base_client import ToolCall
from app.services.agent_tools_nl2sql import (
    ToolResult,
    dispatch_tool_call,
    handle_describe_table,
    handle_execute_sql,
    handle_list_joins,
    handle_list_tables,
    handle_sample_rows,
    TOOL_SCHEMAS,
)


# =============================================================================
# Override autouse warmBusinessObjectRegistry so we can use fake session.
# The conftest fixture has autouse=True and chains to dbSession → seedEngine → real PG.
# We override it here so our tests use fake session without triggering real DB.
# =============================================================================
@pytest_asyncio.fixture(autouse=True)
async def warmBusinessObjectRegistry():
    """No-op override of conftest autouse fixture (no real DB needed for these tests)."""
    yield


# =============================================================================
# Fake rows — mimic SQLAlchemy Row API (supports ._mapping and [0] subscript)
# =============================================================================


class _FakeRow:
    """Fake row that supports both ._mapping dict access and [idx] / .attr access.

    Simulates SQLAlchemy Row behaviour for the columns we care about.
    """

    def __init__(self, data: dict, selected_columns: list[str] | None = None):
        self._data = data
        # selected_columns: column names in SQL SELECT order. If None, use all _data keys in insertion order.
        self._selected_columns = selected_columns if selected_columns is not None else list(data.keys())

    @property
    def _mapping(self):
        """SQLAlchemy Row compatibility: _mapping is a property, not a method."""
        return self._data

    def __getitem__(self, key):
        if isinstance(key, int):
            # Return by SQL SELECT column position
            col_name = self._selected_columns[key]
            return self._data[col_name]
        return self._data[key]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)

    def __iter__(self):
        return (self._data[c] for c in self._selected_columns)

    def __len__(self):
        return len(self._selected_columns)


class _FakeClassRow(_FakeRow):
    """Fake OntologyClass row.

    The selected_columns order must match what the actual SQL query selects.
    For select(OntologyClass.source_table) → use selected_columns=["source_table"]
    so row[0] returns the source_table value.
    """

    def __init__(self, id: int, source_table: str, valid_to=None, selected_columns: list[str] | None = None):
        all_cols = ["id", "source_table", "valid_to"]
        super().__init__(
            data={"id": id, "source_table": source_table, "valid_to": valid_to},
            selected_columns=selected_columns if selected_columns is not None else all_cols,
        )


class _FakePropertyRow(_FakeRow):
    """Fake OntologyProperty row.

    Column order must match SQLAlchemy's SELECT from the table:
    id, class_id, property_name, property_alias, business_aliases, description,
    data_type, is_primary_key, is_foreign_key, ref_class_id, source_column, allowed_values
    """

    def __init__(
        self,
        id: int,
        property_name: str,
        property_alias: str | None,
        data_type: str,
        source_column: str,
        is_primary_key: bool = False,
        is_foreign_key: bool = False,
        ref_class_id: int | None = None,
        class_id: int = 0,
    ):
        super().__init__(
            data={
                "id": id,
                "class_id": class_id,
                "property_name": property_name,
                "property_alias": property_alias,
                "business_aliases": None,
                "description": None,
                "data_type": data_type,
                "is_primary_key": is_primary_key,
                "is_foreign_key": is_foreign_key,
                "ref_class_id": ref_class_id,
                "source_column": source_column,
                "allowed_values": None,
            },
            selected_columns=[
                "id", "class_id", "property_name", "property_alias",
                "business_aliases", "description", "data_type",
                "is_primary_key", "is_foreign_key", "ref_class_id",
                "source_column", "allowed_values",
            ],
        )


class _FakeJoinRow(_FakeRow):
    def __init__(
        self,
        id: int,
        source_class_id: int,
        source_columns: list[str],
        target_class_id: int,
        target_columns: list[str],
        join_type: str = "INNER",
        relation_type: str | None = None,
        description: str | None = None,
    ):
        super().__init__(
            data={
                "id": id,
                "source_class_id": source_class_id,
                "source_columns": source_columns,
                "target_class_id": target_class_id,
                "target_columns": target_columns,
                "join_type": join_type,
                "relation_type": relation_type,
                "description": description,
            },
            selected_columns=[
                "id", "source_class_id", "source_columns", "target_class_id",
                "target_columns", "join_type", "relation_type", "description",
            ],
        )


# =============================================================================
# Fake AsyncSession
# =============================================================================


class _FakeAsyncSession:
    """Fake AsyncSession：支持 execute(stmt) 返回预置行。

    Matches by table name: the SQL is inspected for table names (FROM / JOIN clauses),
    and the corresponding rows are returned. Falls back to keyword substring matching
    for text() SQL queries.
    """

    def __init__(self, rows_by_sql: dict[str, list]):
        # rows_by_sql: table name (lowercase) or keyword → list of rows
        self._rows = rows_by_sql
        self._called: list[str] = []

    async def execute(self, stmt):
        sql_str = str(stmt)
        self._called.append(sql_str)
        sql_lower = sql_str.lower()

        # 1. Try table-name matching: "FROM ontology_class" → key "ontology_class"
        import re
        table_match = re.search(r"FROM\s+(\w+)", sql_lower)
        if table_match:
            table_name = table_match.group(1).lower()
            if table_name in self._rows:
                return _FakeResult(self._rows[table_name])

        # 2. Keyword substring matching (fallback)
        for key, rows in self._rows.items():
            if key.lower() in sql_lower:
                return _FakeResult(rows)

        return _FakeResult([])


class _FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self):
        """Return a fake scalars wrapper that returns _FakeRow objects from all()."""
        return _FakeScalars(self._rows)

    def scalar_one_or_none(self):
        """Return first row as scalar (or None). For SELECT source_table queries."""
        return self._rows[0] if self._rows else None

    def fetchall(self):
        """Return all rows as a list."""
        return self._rows


class _FakeScalars:
    """Fake SQLAlchemy scalars — .all() returns list of _FakeRow objects."""

    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return self._rows


# =============================================================================
# Tests — list_tables
# =============================================================================


@pytest.mark.asyncio
async def test_list_tables_returns_table_names():
    """list_tables 返回表名列表"""
    session = _FakeAsyncSession({
        "source_table": [
            _FakeClassRow(id=1, source_table="THBI.PO_HEADER", selected_columns=["source_table"]),
            _FakeClassRow(id=2, source_table="THBI.PO_LINE", selected_columns=["source_table"]),
        ],
    })
    result = await handle_list_tables(session=session)
    parsed = json.loads(result.content)
    assert "tables" in parsed
    assert isinstance(parsed["tables"], list)
    assert "THBI.PO_HEADER" in parsed["tables"]
    assert "THBI.PO_LINE" in parsed["tables"]


@pytest.mark.asyncio
async def test_list_tables_empty():
    """list_tables 表为空时返回空列表"""
    session = _FakeAsyncSession({})
    result = await handle_list_tables(session=session)
    parsed = json.loads(result.content)
    assert parsed["tables"] == []


# =============================================================================
# Tests — describe_table
# =============================================================================


@pytest.mark.asyncio
async def test_describe_table_returns_columns():
    """describe_table 返回列定义"""
    session = _FakeAsyncSession({
        "ontology_class": [
            _FakeClassRow(id=10, source_table="THBI.PO_HEADER"),
        ],
        "ontology_property": [
            _FakePropertyRow(
                id=1,
                property_name="订单号",
                property_alias="PO Number",
                data_type="VARCHAR2",
                source_column="PTHNUM_0",
                is_primary_key=True,
                class_id=10,
            ),
            _FakePropertyRow(
                id=2,
                property_name="金额",
                property_alias="Amount",
                data_type="NUMBER",
                source_column="CPRPRI_0",
                class_id=10,
            ),
        ],
    })
    result = await handle_describe_table(session=session, table_name="THBI.PO_HEADER")
    parsed = json.loads(result.content)
    assert "columns" in parsed
    assert len(parsed["columns"]) == 2
    col0 = parsed["columns"][0]
    assert col0["name"] == "订单号"
    assert col0["is_primary_key"] is True
    assert col0["source_column"] == "PTHNUM_0"


@pytest.mark.asyncio
async def test_describe_table_not_found():
    """describe_table 表不存在返回 is_error"""
    session = _FakeAsyncSession({})
    result = await handle_describe_table(session=session, table_name="THBI.NOT_EXIST")
    assert result.is_error
    parsed = json.loads(result.content)
    assert parsed["error"] == "TABLE_NOT_FOUND"


# =============================================================================
# Tests — sample_rows
# =============================================================================


@pytest.mark.asyncio
async def test_sample_rows_returns_n_rows():
    """sample_rows 返回前 N 行"""
    # Create a combined session that routes ORM queries to _FakeAsyncSession logic
    # and text SQL queries to the custom _FakeTextResult.
    orm_session = _FakeAsyncSession({
        "ontology_class": [
            _FakeClassRow(id=10, source_table="THBI.PO_HEADER"),
        ],
    })

    class _FakeTextResult:
        def fetchall(self):
            return [
                _FakeRow({"PTHNUM_0": "PO001", "CPRPRI_0": 100.0}),
                _FakeRow({"PTHNUM_0": "PO002", "CPRPRI_0": 200.0}),
                _FakeRow({"PTHNUM_0": "PO003", "CPRPRI_0": 300.0}),
            ]

        def scalar_one_or_none(self):
            return None

        def scalars(self):
            return _FakeScalars([])

    # Wrap the session so ORM queries hit _FakeAsyncSession but text SQL bypasses to _FakeTextResult
    original_execute = orm_session.execute

    async def combined_execute(stmt):
        sql_str = str(stmt)
        if "FETCH FIRST" in sql_str or "FROM" not in sql_str:
            # This is the raw text SQL query for sample rows
            return _FakeTextResult()
        # All other queries (class existence check, etc.) use ORM routing
        return await original_execute(stmt)

    orm_session.execute = combined_execute  # type: ignore

    result = await handle_sample_rows(session=orm_session, table_name="THBI.PO_HEADER", limit=3)
    parsed = json.loads(result.content)
    assert "rows" in parsed
    assert len(parsed["rows"]) == 3
    assert parsed["rows"][0]["PTHNUM_0"] == "PO001"


@pytest.mark.asyncio
async def test_sample_rows_table_not_found():
    """sample_rows 表不存在返回 is_error"""
    session = _FakeAsyncSession({})
    result = await handle_sample_rows(session=session, table_name="THBI.NOT_EXIST")
    assert result.is_error
    parsed = json.loads(result.content)
    assert parsed["error"] == "TABLE_NOT_FOUND"


# =============================================================================
# Tests — execute_sql
# =============================================================================


@pytest.mark.asyncio
async def test_execute_sql_returns_query_result():
    """execute_sql 跑 SELECT 并返回行"""
    session = _FakeAsyncSession({})

    class _FakeExecResult:
        def fetchall(self):
            return [_FakeRow({"x": 1}), _FakeRow({"x": 2})]

    async def fake_execute(stmt):
        return _FakeExecResult()

    session.execute = fake_execute  # type: ignore

    result = await handle_execute_sql(session=session, sql="SELECT 1 AS x")
    assert not result.is_error
    parsed = json.loads(result.content)
    assert parsed["row_count"] == 2
    assert parsed["rows"][0]["x"] == 1


@pytest.mark.asyncio
async def test_execute_sql_rejects_dml():
    """execute_sql 必须拒绝 UPDATE/INSERT/DELETE"""
    session = _FakeAsyncSession({})
    result = await handle_execute_sql(session=session, sql="UPDATE THBI.PO_HEADER SET x=1")
    assert result.is_error
    parsed = json.loads(result.content)
    assert parsed["error"] == "SQL_GUARD_REJECTED"


@pytest.mark.asyncio
async def test_execute_sql_rejects_insert():
    """execute_sql 必须拒绝 INSERT"""
    session = _FakeAsyncSession({})
    result = await handle_execute_sql(session=session, sql="INSERT INTO THBI.PO_HEADER (x) VALUES (1)")
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_sql_rejects_delete():
    """execute_sql 必须拒绝 DELETE"""
    session = _FakeAsyncSession({})
    result = await handle_execute_sql(session=session, sql="DELETE FROM THBI.PO_HEADER")
    assert result.is_error


# =============================================================================
# Tests — list_joins
# =============================================================================


@pytest.mark.asyncio
async def test_list_joins_returns_relationships():
    """list_joins 返回 join 关系"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(
                id=1,
                source_class_id=10,
                source_columns=["PTHNUM_0"],
                target_class_id=20,
                target_columns=["PTHNUM_0"],
                join_type="INNER",
                relation_type=None,
                description="采购订单头行关系",
            ),
        ],
    })
    result = await handle_list_joins(session=session)
    parsed = json.loads(result.content)
    assert "joins" in parsed
    assert len(parsed["joins"]) == 1
    assert parsed["joins"][0]["source_class_id"] == 10
    assert parsed["joins"][0]["target_class_id"] == 20


@pytest.mark.asyncio
async def test_list_joins_empty():
    """list_joins 无数据时返回空列表"""
    session = _FakeAsyncSession({})
    result = await handle_list_joins(session=session)
    parsed = json.loads(result.content)
    assert parsed["joins"] == []


# =============================================================================
# Tests — dispatch_tool_call
# =============================================================================


@pytest.mark.asyncio
async def test_dispatch_tool_call_unknown_tool_returns_error():
    """dispatch_tool_call 收到未知 tool 名返回 is_error"""
    session = _FakeAsyncSession({})
    tc = ToolCall(id="t1", name="unknown_tool", args={})
    result = await dispatch_tool_call(tc, session=session)
    assert result.is_error
    assert result.tool_call_id == "t1"
    parsed = json.loads(result.content)
    assert parsed["error"] == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_dispatch_tool_call_routes_to_correct_handler():
    """dispatch_tool_call 正确路由 list_tables"""
    session = _FakeAsyncSession({
        "source_table": [
            _FakeClassRow(id=1, source_table="THBI.PO_HEADER"),
        ],
    })
    tc = ToolCall(id="t2", name="list_tables", args={})
    result = await dispatch_tool_call(tc, session=session)
    assert result.name == "list_tables"
    assert not result.is_error
    assert result.tool_call_id == "t2"


@pytest.mark.asyncio
async def test_dispatch_tool_call_routes_describe_table():
    """dispatch_tool_call 正确路由 describe_table"""
    session = _FakeAsyncSession({
        "source_table = 'THBI.PO_HEADER'": [
            _FakeClassRow(id=10, source_table="THBI.PO_HEADER"),
        ],
        "class_id = 10": [],
    })
    tc = ToolCall(id="t3", name="describe_table", args={"table_name": "THBI.PO_HEADER"})
    result = await dispatch_tool_call(tc, session=session)
    assert result.name == "describe_table"
    assert result.tool_call_id == "t3"


@pytest.mark.asyncio
async def test_dispatch_tool_call_routes_sample_rows():
    """dispatch_tool_call 正确路由 sample_rows"""
    session = _FakeAsyncSession({
        "source_table = 'THBI.PO_HEADER'": [
            _FakeClassRow(id=10, source_table="THBI.PO_HEADER"),
        ],
    })

    class _FakeExecResult:
        def fetchall(self):
            return [{"PTHNUM_0": "PO001"}]

    async def fake_execute(stmt):
        return _FakeExecResult()

    session.execute = fake_execute  # type: ignore

    tc = ToolCall(id="t4", name="sample_rows", args={"table_name": "THBI.PO_HEADER", "limit": 5})
    result = await dispatch_tool_call(tc, session=session)
    assert result.name == "sample_rows"
    assert result.tool_call_id == "t4"


@pytest.mark.asyncio
async def test_dispatch_tool_call_routes_execute_sql():
    """dispatch_tool_call 正确路由 execute_sql"""
    session = _FakeAsyncSession({})

    class _FakeExecResult:
        def fetchall(self):
            return [{"a": 1}]

    async def fake_execute(stmt):
        return _FakeExecResult()

    session.execute = fake_execute  # type: ignore

    tc = ToolCall(id="t5", name="execute_sql", args={"sql": "SELECT 1 AS a"})
    result = await dispatch_tool_call(tc, session=session)
    assert result.name == "execute_sql"
    assert result.tool_call_id == "t5"


@pytest.mark.asyncio
async def test_dispatch_tool_call_routes_list_joins():
    """dispatch_tool_call 正确路由 list_joins"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(
                id=1,
                source_class_id=10,
                source_columns=["COL1"],
                target_class_id=20,
                target_columns=["COL1"],
            ),
        ],
    })
    tc = ToolCall(id="t6", name="list_joins", args={})
    result = await dispatch_tool_call(tc, session=session)
    assert result.name == "list_joins"
    assert result.tool_call_id == "t6"


# =============================================================================
# Tests — TOOL_SCHEMAS
# =============================================================================


def test_tool_schemas_has_5_entries():
    """TOOL_SCHEMAS 包含 5 个工具"""
    assert len(TOOL_SCHEMAS) == 5
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert names == {"list_tables", "describe_table", "sample_rows", "execute_sql", "list_joins"}


def test_tool_schemas_execute_sql_has_sql_required():
    """execute_sql schema 的 sql 参数为 required"""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "execute_sql")
    assert "sql" in schema["function"]["parameters"]["required"]


def test_tool_schemas_sample_rows_has_default_limit():
    """sample_rows schema 的 limit 有默认值 5"""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "sample_rows")
    props = schema["function"]["parameters"]["properties"]
    assert props["limit"]["default"] == 5


# =============================================================================
# Tests — ToolResult frozen
# =============================================================================


def test_tool_result_is_frozen():
    """ToolResult 是 frozen dataclass，不可修改"""
    result = ToolResult(tool_call_id="x", name="y", content="z")
    with pytest.raises(Exception):  # frozen dataclass 不可赋值
        result.is_error = True  # type: ignore
