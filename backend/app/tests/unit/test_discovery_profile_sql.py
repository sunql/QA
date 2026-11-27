"""确定性剖析 SQL 的纯函数（WP-ONT-AI A2 / G3-1）。

钉住四件事：

1. **注入面**：表名/列名来自**外部 schema**（不可信）⇒ 只接受严格标识符形态，
   再按方言加引号；`T"; DROP TABLE X; --` 必须在拼 SQL **之前**被拒。
2. **聚合可算性按类型裁剪**：LOB / JSON 不能 `COUNT(DISTINCT)` 或 `MIN/MAX`；
   BOOLEAN 不能 `MIN/MAX`。发出去就报错 ⇒ 整次剖析失败，这是「噪声 vs 崩溃」的选择。
3. **分块确定**：列按名字排序后按 `PROFILE_CHUNK_SIZE` 切块（Oracle 单 SELECT
   列表达式有上限）；同一快照两次剖析必须切出**同样的块** ⇒ `sql_digest` 可复现。
4. **摘要口径**：`sql_digest` = 规范化 SQL 的 SHA-256（与快照/Manifest 同一把尺子）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.services.discovery_profile_sql import (
    PROFILE_CHUNK_SIZE,
    ProfileSqlError,
    buildColumnStatsSql,
    buildTableCountSql,
    chunkColumns,
    parseColumnStats,
    parseTableCount,
    profileColumnsFromSnapshot,
    sqlDigest,
    stringifyValue,
)


def _column(name: str, dataType: str) -> dict[str, str]:
    return {"columnName": name, "dataType": dataType}


def _table(columns: list[dict[str, str]], *, owner: str = "THBI") -> dict:
    return {"tableName": "SUPPLIER", "owner": owner, "columns": columns}


# ---------------------------------------------------------------------------
# 定位符与引号
# ---------------------------------------------------------------------------


def test_identifier_is_quoted_per_dialect() -> None:
    oracle = buildTableCountSql("SUPPLIER", "THBI", dialect="oracle")
    mysql = buildTableCountSql("SUPPLIER", "THBI", dialect="mysql")

    assert 'FROM "THBI"."SUPPLIER"' in oracle
    assert "FROM `THBI`.`SUPPLIER`" in mysql


def test_missing_owner_leaves_the_table_unqualified() -> None:
    sql = buildTableCountSql("SUPPLIER", None, dialect="oracle")

    assert 'FROM "SUPPLIER"' in sql


@pytest.mark.parametrize(
    "badName",
    [
        'T"; DROP TABLE X; --',
        "T NAME",
        "1TABLE",
        "T-1",
        "",
        "A" * 129,
    ],
)
def test_identifier_validation_rejects_non_identifier(badName: str) -> None:
    with pytest.raises(ProfileSqlError):
        buildTableCountSql(badName, "THBI", dialect="oracle")


def test_column_identifier_is_validated_too() -> None:
    table = _table([_column('C"; DELETE FROM X', "NUMBER")])

    with pytest.raises(ProfileSqlError):
        buildColumnStatsSql(
            "SUPPLIER", "THBI", profileColumnsFromSnapshot(table), dialect="oracle"
        )


# ---------------------------------------------------------------------------
# 聚合表达式
# ---------------------------------------------------------------------------


def test_table_count_sql_counts_all_rows() -> None:
    sql = buildTableCountSql("SUPPLIER", "THBI", dialect="oracle")

    assert sql.upper().startswith("SELECT COUNT(*)")


def test_each_column_gets_counts_and_range_aggregates() -> None:
    table = _table([_column("BPSNUM_0", "VARCHAR2"), _column("QTY", "NUMBER")])
    columns = profileColumnsFromSnapshot(table)

    sql = buildColumnStatsSql("SUPPLIER", "THBI", columns, dialect="oracle")

    assert 'COUNT(*) - COUNT("BPSNUM_0") AS "N0"' in sql
    assert 'COUNT(DISTINCT "BPSNUM_0") AS "D0"' in sql
    assert 'MIN("BPSNUM_0") AS "MIN0"' in sql
    assert 'MAX("BPSNUM_0") AS "MAX0"' in sql
    assert 'COUNT(DISTINCT "QTY") AS "D1"' in sql


@pytest.mark.parametrize(
    ("dataType", "needDistinct", "needRange"),
    [
        ("VARCHAR2(100)", True, True),
        ("NUMBER(18,4)", True, True),
        ("TIMESTAMP(6)", True, True),
        ("CLOB", False, False),
        ("BLOB", False, False),
        ("jsonb", False, False),
        ("BOOLEAN", True, False),
        ("RAW(16)", False, False),
    ],
)
def test_aggregate_support_is_decided_by_type(
    dataType: str, needDistinct: bool, needRange: bool
) -> None:
    column = profileColumnsFromSnapshot(_table([_column("C1", dataType)]))[0]

    assert column.supportsDistinct is needDistinct
    assert column.supportsRange is needRange


def test_unsupported_aggregates_are_omitted_from_sql() -> None:
    """**行为升级（2026-11-27）**：CLOB 现在整列被剔除（含空值计数）——
    Oracle 连 `COUNT(clob_col)` 都报 ORA-00932（用户实测），旧裁剪只去掉
    DISTINCT/MIN/MAX 是不够的。本用例改为钉「构造层拒绝整列不可聚合的列」；
    「JSON 这类 supportsDistinct=False 但可 COUNT 的类型仍只做空值计数」
    由 test_discovery_unprofilable_tables.py::test_null_count_only_column_produces_count_only_sql 覆盖。
    """
    columns = profileColumnsFromSnapshot(_table([_column("PAYLOAD", "CLOB")]))

    with pytest.raises(ProfileSqlError, match="整列不可聚合"):
        buildColumnStatsSql("SUPPLIER", "THBI", columns, dialect="oracle")


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------


def test_chunks_are_sorted_and_sized() -> None:
    names = [f"C{index:03d}" for index in range(PROFILE_CHUNK_SIZE + 5)]
    table = _table([_column(name, "NUMBER") for name in reversed(names)])

    chunks = chunkColumns(profileColumnsFromSnapshot(table))

    assert [len(chunk) for chunk in chunks] == [PROFILE_CHUNK_SIZE, 5]
    assert [column.name for column in chunks[0]] == names[:PROFILE_CHUNK_SIZE]
    assert [column.name for column in chunks[1]] == names[PROFILE_CHUNK_SIZE:]


def test_empty_column_list_yields_no_chunks() -> None:
    assert chunkColumns(profileColumnsFromSnapshot(_table([]))) == ()


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def test_digest_is_sha256_and_stable() -> None:
    sql = buildTableCountSql("SUPPLIER", "THBI", dialect="oracle")

    digest = sqlDigest(sql)

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest == sqlDigest(sql)


def test_digest_changes_when_the_sql_changes() -> None:
    one = sqlDigest(buildTableCountSql("SUPPLIER", "THBI", dialect="oracle"))
    two = sqlDigest(buildTableCountSql("ITEM", "THBI", dialect="oracle"))

    assert one != two


def test_column_chunk_sql_is_deterministic_regardless_of_input_order() -> None:
    columns = profileColumnsFromSnapshot(
        _table([_column("B", "NUMBER"), _column("A", "NUMBER")])
    )
    reordered = profileColumnsFromSnapshot(
        _table([_column("A", "NUMBER"), _column("B", "NUMBER")])
    )

    first = buildColumnStatsSql("SUPPLIER", "THBI", columns, dialect="oracle")
    second = buildColumnStatsSql("SUPPLIER", "THBI", reordered, dialect="oracle")

    assert first == second


# ---------------------------------------------------------------------------
# 结果解析
# ---------------------------------------------------------------------------


def test_parse_table_count_is_case_insensitive() -> None:
    assert parseTableCount([{"total_rows": 42}]) == 42
    assert parseTableCount([{"TOTAL_ROWS": Decimal("42")}]) == 42


def test_parse_column_stats_maps_aliases_and_missing_aggregates() -> None:
    columns = profileColumnsFromSnapshot(
        _table(
            [
                _column("A", "NUMBER"),
                _column("B", "CLOB"),
            ]
        )
    )
    row = {
        "N0": 3,
        "D0": 2,
        "MIN0": Decimal("1.50"),
        "MAX0": Decimal("9.00"),
        "N1": 5,
    }

    stats = parseColumnStats([row], columns)

    assert stats[0].column == "A"
    assert stats[0].nullCount == 3
    assert stats[0].distinctCount == 2
    assert stats[0].minValue == "1.50"
    assert stats[1].column == "B"
    assert stats[1].nullCount == 5
    assert stats[1].distinctCount is None
    assert stats[1].minValue is None


def test_parse_column_stats_requires_a_row() -> None:
    columns = profileColumnsFromSnapshot(_table([_column("A", "NUMBER")]))

    with pytest.raises(ProfileSqlError):
        parseColumnStats([], columns)


def test_stringify_value_shapes_and_truncates() -> None:
    # 保留精度：1.5000 不能变成 1.5（值域是证据，不改写原值）
    assert stringifyValue(Decimal("12.3400")) == "12.3400"
    assert stringifyValue(Decimal("1.5000")) == "1.5000"
    assert stringifyValue(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert stringifyValue("x" * 500) == "x" * 200
    assert stringifyValue(None) is None
