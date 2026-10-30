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
    DESCRIBE_TABLE_COLUMN_FORMAT,
    L4_TOOL_ROW_CAP,
    ToolResult,
    dispatch_tool_call,
    encodeExecuteSqlResult,
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


@pytest.mark.asyncio
async def test_list_tables_dedupes_repeated_source_table():
    """同一 source_table 对应多个 ontology_class 时只列一次（去重保序）。"""
    session = _FakeAsyncSession({
        "source_table": [
            _FakeClassRow(id=1, source_table="THBI.PO_HEADER", selected_columns=["source_table"]),
            _FakeClassRow(id=2, source_table="THBI.PO_HEADER", selected_columns=["source_table"]),
            _FakeClassRow(id=3, source_table="THBI.PO_LINE", selected_columns=["source_table"]),
        ],
    })
    result = await handle_list_tables(session=session)
    assert json.loads(result.content)["tables"] == ["THBI.PO_HEADER", "THBI.PO_LINE"]


# =============================================================================
# Tests — describe_table（列式紧凑编码）
# =============================================================================


@pytest.mark.asyncio
async def test_describe_table_returns_compact_columns():
    """describe_table 列式返回：自描述 column_format + 每列一行数组。"""
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
    assert parsed["table"] == "THBI.PO_HEADER"
    assert parsed["column_format"] == DESCRIBE_TABLE_COLUMN_FORMAT
    assert parsed["columns"] == [
        ["订单号", "PO Number", "VARCHAR2", "PTHNUM_0", "PK", ""],
        ["金额", "Amount", "NUMBER", "CPRPRI_0", "", ""],
    ]


@pytest.mark.asyncio
async def test_describe_table_omits_alias_equal_to_name_or_source_column():
    """alias 与 name / source_column 相同时置空，避免重复 token。"""
    session = _FakeAsyncSession({
        "ontology_class": [_FakeClassRow(id=10, source_table="THBI.PO_HEADER")],
        "ontology_property": [
            _FakePropertyRow(
                id=1, property_name="PTHNUM_0", property_alias="PTHNUM_0",
                data_type="VARCHAR2", source_column="PTHNUM_0", class_id=10,
            ),
            _FakePropertyRow(
                id=2, property_name="订单号", property_alias=None,
                data_type="VARCHAR2", source_column="PTHNUM_0", class_id=10,
            ),
        ],
    })
    parsed = json.loads(
        (await handle_describe_table(session=session, table_name="THBI.PO_HEADER")).content
    )
    assert [row[1] for row in parsed["columns"]] == ["", ""]


@pytest.mark.asyncio
async def test_describe_table_marks_pk_and_fk_flags():
    """PK / FK 用固定序逗号拼接（PK,FK）。"""
    session = _FakeAsyncSession({
        "ontology_class": [_FakeClassRow(id=10, source_table="THBI.PO_LINE")],
        "ontology_property": [
            _FakePropertyRow(
                id=1, property_name="行号", property_alias="Line",
                data_type="NUMBER", source_column="PTHLIN_0",
                is_primary_key=True, is_foreign_key=True, class_id=10,
            ),
        ],
    })
    parsed = json.loads(
        (await handle_describe_table(session=session, table_name="THBI.PO_LINE")).content
    )
    assert parsed["columns"][0][4] == "PK,FK"


@pytest.mark.asyncio
async def test_describe_table_resolves_ref_class_to_table_name():
    """ref_class_id 解析成被引用表名（class_id 无法被任何其它工具消费）。"""
    session = _FakeAsyncSession({
        "ontology_class": [
            _FakeClassRow(id=10, source_table="THBI.PO_LINE"),
            _FakeClassRow(id=20, source_table="THBI.PO_HEADER"),
        ],
        "ontology_property": [
            _FakePropertyRow(
                id=1, property_name="订单号", property_alias="PO No",
                data_type="VARCHAR2", source_column="PTHNUM_0",
                is_foreign_key=True, ref_class_id=20, class_id=10,
            ),
        ],
    })
    parsed = json.loads(
        (await handle_describe_table(session=session, table_name="THBI.PO_LINE")).content
    )
    assert parsed["columns"][0][5] == "THBI.PO_HEADER"


@pytest.mark.asyncio
async def test_describe_table_unresolvable_ref_keeps_empty_ref():
    """ref_class_id 查不到表名（软删类等）→ ref 留空，FK 标记仍保留。"""
    session = _FakeAsyncSession({
        "ontology_class": [_FakeClassRow(id=10, source_table="THBI.PO_LINE")],
        "ontology_property": [
            _FakePropertyRow(
                id=1, property_name="订单号", property_alias="PO No",
                data_type="VARCHAR2", source_column="PTHNUM_0",
                is_foreign_key=True, ref_class_id=999, class_id=10,
            ),
        ],
    })
    parsed = json.loads(
        (await handle_describe_table(session=session, table_name="THBI.PO_LINE")).content
    )
    assert parsed["columns"][0][4] == "FK"
    assert parsed["columns"][0][5] == ""


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
async def test_execute_sql_returns_columnar_result():
    """execute_sql 列式返回：columns 一次 + 值数组。"""
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
    assert parsed["columns"] == ["x"]
    assert parsed["rows"] == [[1], [2]]
    assert parsed["row_count"] == 2
    assert "truncated" not in parsed


def _rows(n: int) -> list[_FakeRow]:
    return [_FakeRow({"x": i}) for i in range(n)]


@pytest.mark.asyncio
async def test_execute_sql_caps_rows_and_reports_true_count():
    """超过 L4_TOOL_ROW_CAP 行时只发前 100 行，row_count 仍报真实总数。"""
    session = _FakeAsyncSession({})
    rows = _rows(286)

    class _FakeExecResult:
        def fetchall(self):
            return rows

    async def fake_execute(stmt):
        return _FakeExecResult()

    session.execute = fake_execute  # type: ignore

    parsed = json.loads(
        (await handle_execute_sql(session=session, sql="SELECT x FROM T")).content
    )
    assert len(parsed["rows"]) == L4_TOOL_ROW_CAP == 100
    assert parsed["row_count"] == 286
    assert parsed["truncated"] is True
    assert "100" in parsed["note"] and "286" in parsed["note"]


@pytest.mark.asyncio
async def test_execute_sql_full_result_has_no_truncation_markers():
    """未超限时不带 truncated/note，避免 LLM 误判数据不完整。"""
    session = _FakeAsyncSession({})

    class _FakeExecResult:
        def fetchall(self):
            return _rows(3)

    async def fake_execute(stmt):
        return _FakeExecResult()

    session.execute = fake_execute  # type: ignore

    parsed = json.loads(
        (await handle_execute_sql(session=session, sql="SELECT x FROM T")).content
    )
    assert "truncated" not in parsed
    assert "note" not in parsed


def test_encode_execute_sql_result_unions_ragged_row_keys():
    """不规则行（部分 dict 缺键）→ 列取并集，缺失值补 None。"""
    parsed = encodeExecuteSqlResult([{"a": 1}, {"b": 2}, {"a": 3, "c": 4}])
    assert parsed["columns"] == ["a", "b", "c"]
    assert parsed["rows"] == [[1, None, None], [None, 2, None], [3, None, 4]]


def test_encode_execute_sql_result_does_not_mutate_input():
    """编码器必须返回新对象，不修改调用方的行字典/列表。"""
    rows = [{"a": 1}]
    parsed = encodeExecuteSqlResult(rows)
    parsed["rows"].append([9])
    assert rows == [{"a": 1}]


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


def _joinClasses() -> list:
    """list_joins 测试共用的 class_id → source_table 映射。"""
    return [
        _FakeClassRow(id=10, source_table="THBI.PO_HEADER"),
        _FakeClassRow(id=20, source_table="THBI.PO_LINE"),
    ]


@pytest.mark.asyncio
async def test_list_joins_renders_table_column_edges():
    """list_joins 输出 TABLE.COL -> TABLE.COL 文本边（class_id 对 LLM 无意义）。"""
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
        "ontology_class": _joinClasses(),
    })
    result = await handle_list_joins(session=session)
    parsed = json.loads(result.content)
    assert parsed["count"] == 1
    assert parsed["edges"] == [
        "THBI.PO_HEADER.PTHNUM_0 -> THBI.PO_LINE.PTHNUM_0 # 采购订单头行关系"
    ]


@pytest.mark.asyncio
async def test_list_joins_marks_non_inner_join_type():
    """非 INNER 连接追加 (LEFT) 等标记，否则 JOIN 语义丢失。"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(
                id=1, source_class_id=10, source_columns=["A"],
                target_class_id=20, target_columns=["A"], join_type="LEFT",
            ),
        ],
        "ontology_class": _joinClasses(),
    })
    parsed = json.loads((await handle_list_joins(session=session)).content)
    assert parsed["edges"] == ["THBI.PO_HEADER.A -> THBI.PO_LINE.A (LEFT)"]


@pytest.mark.asyncio
async def test_list_joins_dedupes_reverse_duplicate_edges():
    """同一对表的反向重复边（A→B 与 B→A 同列）只保留一条。"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(id=1, source_class_id=10, source_columns=["PTHNUM_0"],
                         target_class_id=20, target_columns=["PTHNUM_0"]),
            _FakeJoinRow(id=2, source_class_id=20, source_columns=["PTHNUM_0"],
                         target_class_id=10, target_columns=["PTHNUM_0"]),
        ],
        "ontology_class": _joinClasses(),
    })
    parsed = json.loads((await handle_list_joins(session=session)).content)
    assert parsed["count"] == 1


@pytest.mark.asyncio
async def test_list_joins_keeps_parallel_edges_with_different_columns():
    """同表对不同列的多条边是真实语义（JOIN 边扇出），不能按表对去重。"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(id=1, source_class_id=10, source_columns=["PTHNUM_0"],
                         target_class_id=20, target_columns=["PTHNUM_0"]),
            _FakeJoinRow(id=2, source_class_id=10, source_columns=["PTHLIN_0"],
                         target_class_id=20, target_columns=["PTHLIN_0"]),
        ],
        "ontology_class": _joinClasses(),
    })
    parsed = json.loads((await handle_list_joins(session=session)).content)
    assert parsed["count"] == 2
    assert "THBI.PO_HEADER.PTHLIN_0 -> THBI.PO_LINE.PTHLIN_0" in parsed["edges"]


@pytest.mark.asyncio
async def test_list_joins_filters_by_tables_param():
    """tables 过滤：只保留任一端命中的边；大小写不敏感、支持裸表名。"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(id=1, source_class_id=10, source_columns=["A"],
                         target_class_id=20, target_columns=["A"]),
            _FakeJoinRow(id=2, source_class_id=20, source_columns=["B"],
                         target_class_id=30, target_columns=["B"]),
        ],
        "ontology_class": _joinClasses() + [
            _FakeClassRow(id=30, source_table="THBI.DIM_SUPPLIER"),
        ],
    })
    parsed = json.loads(
        (await handle_list_joins(session=session, tables=["po_line"])).content
    )
    assert parsed["count"] == 2  # 两条边都涉及 PO_LINE
    parsed2 = json.loads(
        (await handle_list_joins(session=session, tables=["THBI.DIM_SUPPLIER"])).content
    )
    assert parsed2["count"] == 1
    assert "DIM_SUPPLIER" in parsed2["edges"][0]


@pytest.mark.asyncio
async def test_list_joins_accepts_tables_as_plain_string():
    """LLM 误把数组传成字符串时，按单个表名过滤而不是逐字符迭代。"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(id=1, source_class_id=10, source_columns=["A"],
                         target_class_id=20, target_columns=["A"]),
            _FakeJoinRow(id=2, source_class_id=20, source_columns=["B"],
                         target_class_id=30, target_columns=["B"]),
        ],
        "ontology_class": _joinClasses() + [
            _FakeClassRow(id=30, source_table="THBI.DIM_SUPPLIER"),
        ],
    })
    parsed = json.loads(
        (await handle_list_joins(session=session, tables="THBI.DIM_SUPPLIER")).content
    )
    assert parsed["count"] == 1
    assert "DIM_SUPPLIER" in parsed["edges"][0]


@pytest.mark.asyncio
async def test_list_joins_skips_edges_with_unresolvable_endpoint():
    """端点类查不到表名（软删 / 越权）→ 跳过该边，不输出无法使用的 class_id。"""
    session = _FakeAsyncSession({
        "ontology_join": [
            _FakeJoinRow(id=1, source_class_id=10, source_columns=["A"],
                         target_class_id=999, target_columns=["A"]),
        ],
        "ontology_class": _joinClasses(),
    })
    parsed = json.loads((await handle_list_joins(session=session)).content)
    assert parsed["count"] == 0
    assert parsed["edges"] == []


@pytest.mark.asyncio
async def test_list_joins_empty():
    """list_joins 无数据时返回空列表"""
    session = _FakeAsyncSession({})
    result = await handle_list_joins(session=session)
    parsed = json.loads(result.content)
    assert parsed["edges"] == []
    assert parsed["count"] == 0


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


def test_tool_schemas_list_joins_has_optional_tables():
    """list_joins 增加可选 tables 过滤参数（不传返回全部）。"""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "list_joins")
    props = schema["function"]["parameters"]["properties"]
    assert props["tables"]["type"] == "array"
    assert props["tables"]["items"]["type"] == "string"
    assert "required" not in schema["function"]["parameters"]


def test_tool_schemas_execute_sql_mentions_row_cap():
    """execute_sql 描述必须告知 100 行上限 + 聚合下推。"""
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "execute_sql")
    desc = schema["function"]["description"]
    assert "100" in desc
    assert "聚合" in desc


# =============================================================================
# Tests — ToolResult frozen
# =============================================================================


def test_tool_result_is_frozen():
    """ToolResult 是 frozen dataclass，不可修改"""
    result = ToolResult(tool_call_id="x", name="y", content="z")
    with pytest.raises(Exception):  # frozen dataclass 不可赋值
        result.is_error = True  # type: ignore
