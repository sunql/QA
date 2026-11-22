"""确定性剖析 SQL 的**纯函数**（WP-ONT-AI A2 / G3-1）。

为什么先有纯函数
----------------
剖析要回答的是「这张表有多少行、每列多少空值、多少不同值、值域多大」。
这些数字来自**外部业务库**，而表名/列名同样来自外部 schema（不可信）——
拼 SQL 是唯一的安全敏感点。把它做成纯函数，就能在**不连库**的前提下把：

- 标识符白名单（拒绝 `T"; DROP TABLE X; --` 这类形态）；
- 聚合可算性（LOB / JSON 不能 `COUNT(DISTINCT)` 或 `MIN/MAX`，BOOLEAN 不能 `MIN/MAX`）；
- 分块与排序（同一快照两次剖析必须切出**同样的块**）；
- `sql_digest`（规范化 SQL 的 SHA-256，与快照/Manifest 同一把尺子）

逐条钉死。IO 在 `discovery_profiler.py`。

## 为什么按类型裁剪聚合

`SELECT MIN(clob_col)` 在 Oracle 直接报 ORA-00932，`COUNT(DISTINCT jsonb)` 在 PG 报
`could not identify an equality operator`。发出去就是**整次剖析失败**——
而失败一条 LOB 列不值得让 229 列的表全丢。⇒ 不能算的聚合**根本不发**，
结果里该字段为 NULL（`distinct_count IS NULL` 的含义是「算不了」，不是「0」）。

## 为什么分块

Oracle 单 SELECT 的选择列表有上限（1000 列表达式），真实采购表最高 248 列 ——
每列 4 个聚合表达式会直接越界。按 `PROFILE_CHUNK_SIZE` 块切，块内列按名字排序
⇒ 同一快照的块划分与列序**完全确定**。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

#: 单条聚合 SQL 最多带多少列（每列最多 4 个表达式 ⇒ 80 个表达式，远低于 Oracle 的 1000）。
PROFILE_CHUNK_SIZE = 20

#: 标识符最大长度（与 `discovery_profile.table_name` / `column_name` 的列宽一致）。
MAX_IDENTIFIER_LENGTH = 128

#: 值域列的展示宽度（`discovery_profile.min_value` / `max_value` 是 String(200)）。
MAX_VALUE_LENGTH = 200

#: 严格标识符：字母/下划线开头，后续字母数字/`$`/`#`（Oracle 允许后两者）。
#: 引号类字符一律不在白名单内 ⇒ 拼进 SQL 前就已无法闭合标识符。
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

#: 不能 `COUNT(DISTINCT)` 的类型（大对象 / 二进制 / 无等值算子 / 结构化类型）。
_UNSUPPORTED_DISTINCT = frozenset(
    {
        "CLOB",
        "NCLOB",
        "BLOB",
        "BFILE",
        "LONG",
        "XMLTYPE",
        "XML",
        "JSON",
        "JSONB",
        "BYTEA",
        "IMAGE",
        "GEOMETRY",
        "GEOGRAPHY",
        "RAW",
        "BINARY",
        "VARBINARY",
    }
)

#: 不能 `MIN/MAX` 的类型 = 上表 + 无序类型。
_UNSUPPORTED_RANGE = _UNSUPPORTED_DISTINCT | frozenset({"BOOLEAN", "BOOL", "BIT"})

_MYSQL_DIALECTS = frozenset({"mysql", "mariadb"})


class ProfileSqlError(ValueError):
    """剖析 SQL 无法安全构造 / 结果无法解析（**不是**数据库错误）。"""


@dataclass(frozen=True, slots=True)
class ProfileColumn:
    """一列的剖析能力（由快照里的 `dataType` 决定）。"""

    name: str
    dataType: str
    supportsDistinct: bool
    supportsRange: bool


@dataclass(frozen=True, slots=True)
class ColumnStats:
    """一列的聚合结果；`None` = **该聚合不适用**（不是 0）。"""

    column: str
    nullCount: int | None = None
    distinctCount: int | None = None
    minValue: str | None = None
    maxValue: str | None = None


# ---------------------------------------------------------------------------
# 快照 → 列能力
# ---------------------------------------------------------------------------


def profileColumnsFromSnapshot(table: Mapping[str, Any]) -> tuple[ProfileColumn, ...]:
    """从快照的一张表里取出**按名字排序**的列及其聚合能力。

    ⚠️ 排序在这里而不是在调用方：块划分必须与调用方给列的顺序无关，
    否则「同一快照两次剖析的 `sql_digest` 不同」——证据不可复算。
    """
    rawColumns = table.get("columns")
    if not isinstance(rawColumns, list):
        return ()
    columns: list[ProfileColumn] = []
    for item in rawColumns:
        if not isinstance(item, Mapping):
            continue
        name = item.get("columnName")
        dataType = item.get("dataType")
        assertIdentifier(name, what="列名")
        baseType = _baseType(dataType)
        supportsDistinct = baseType not in _UNSUPPORTED_DISTINCT
        columns.append(
            ProfileColumn(
                name=str(name),
                dataType=str(dataType or ""),
                supportsDistinct=supportsDistinct,
                supportsRange=supportsDistinct and baseType not in _UNSUPPORTED_RANGE,
            )
        )
    columns.sort(key=lambda column: column.name)
    return tuple(columns)


def chunkColumns(
    columns: Sequence[ProfileColumn], *, size: int = PROFILE_CHUNK_SIZE
) -> tuple[tuple[ProfileColumn, ...], ...]:
    """把列切成固定大小的块（列先按名字排序 ⇒ 块划分确定）。"""
    if size <= 0:
        raise ProfileSqlError(f"分块大小必须为正整数（收到 {size}）")
    ordered = sorted(columns, key=lambda column: column.name)
    return tuple(
        tuple(ordered[start : start + size]) for start in range(0, len(ordered), size)
    )


# ---------------------------------------------------------------------------
# SQL 构造
# ---------------------------------------------------------------------------


def buildTableCountSql(
    tableName: str, owner: str | None, *, dialect: str
) -> str:
    """表级行数：`SELECT COUNT(*) FROM owner.table`。"""
    return f"SELECT COUNT(*) AS {_quote('TOTAL_ROWS', dialect)} FROM {_qualified(tableName, owner, dialect)}"


def buildColumnStatsSql(
    tableName: str,
    owner: str | None,
    columns: Sequence[ProfileColumn],
    *,
    dialect: str,
) -> str:
    """一块列的聚合：空值数 / 不同值数 / 最小 / 最大（按列能力裁剪）。"""
    if not columns:
        raise ProfileSqlError("聚合 SQL 至少需要一列")
    expressions: list[str] = []
    for index, column in enumerate(columns):
        quoted = _quote(validateIdentifier(column.name), dialect)
        expressions.append(
            f"COUNT(*) - COUNT({quoted}) AS {_quote(f'N{index}', dialect)}"
        )
        if column.supportsDistinct:
            expressions.append(
                f"COUNT(DISTINCT {quoted}) AS {_quote(f'D{index}', dialect)}"
            )
        if column.supportsRange:
            expressions.append(f"MIN({quoted}) AS {_quote(f'MIN{index}', dialect)}")
            expressions.append(f"MAX({quoted}) AS {_quote(f'MAX{index}', dialect)}")
    return f"SELECT {', '.join(expressions)} FROM {_qualified(tableName, owner, dialect)}"


def sqlDigest(sql: str) -> str:
    """规范化 SQL 的 SHA-256（与快照哈希同一种摘要）。"""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 结果解析
# ---------------------------------------------------------------------------


def parseTableCount(rows: Sequence[Mapping[str, Any]]) -> int:
    value = _rowValue(rows, "TOTAL_ROWS")
    return _asInt(value, "TOTAL_ROWS")


def parseColumnStats(
    rows: Sequence[Mapping[str, Any]], columns: Sequence[ProfileColumn]
) -> tuple[ColumnStats, ...]:
    """按 `N{i}` / `D{i}` / `MIN{i}` / `MAX{i}` 取回每列的聚合。"""
    if not rows:
        raise ProfileSqlError("聚合查询没有返回任何行")
    row = {str(key).upper(): value for key, value in rows[0].items()}
    stats: list[ColumnStats] = []
    for index, column in enumerate(columns):
        supportsDistinct = column.supportsDistinct
        supportsRange = column.supportsRange
        stats.append(
            ColumnStats(
                column=column.name,
                nullCount=_asInt(row.get(f"N{index}"), f"N{index}"),
                distinctCount=(
                    _asInt(row.get(f"D{index}"), f"D{index}")
                    if supportsDistinct
                    else None
                ),
                minValue=(
                    stringifyValue(row.get(f"MIN{index}")) if supportsRange else None
                ),
                maxValue=(
                    stringifyValue(row.get(f"MAX{index}")) if supportsRange else None
                ),
            )
        )
    return tuple(stats)


def stringifyValue(value: Any) -> str | None:
    """聚合值 → 列宽内的字符串（**保留数值精度**，时间走 ISO）。

    ⚠️ 刻意不做 `Decimal.normalize()`：`NUMERIC(18,4)` 的 `1.5000` 去尾零会变成
    `1.5`，看起来一样但丢了「这一列声明了 4 位小数」的信息；值域是证据，不该改写。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, (datetime, date, time)):
        text = value.isoformat()
    elif isinstance(value, bytes):
        # 二进制列不进值域聚合；真出现了也不把原始字节塞进报告。
        return None
    else:
        text = str(value)
    return text[:MAX_VALUE_LENGTH]


# ---------------------------------------------------------------------------
# 内部：标识符与引号
# ---------------------------------------------------------------------------


def assertIdentifier(value: Any, *, what: str) -> str:
    """校验并返回标识符；不合法一律 `ProfileSqlError`（拼 SQL 之前拒）。"""
    if not isinstance(value, str) or not value:
        raise ProfileSqlError(f"{what}必须是非空字符串（收到 {value!r}）")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ProfileSqlError(f"{what}过长：{len(value)} > {MAX_IDENTIFIER_LENGTH}")
    if not _IDENTIFIER_PATTERN.match(value):
        raise ProfileSqlError(f"{what}不是合法标识符：{value!r}")
    return value


def validateIdentifier(value: str) -> str:
    return assertIdentifier(value, what="列名")


def _qualified(tableName: str, owner: str | None, dialect: str) -> str:
    table = _quote(assertIdentifier(tableName, what="表名"), dialect)
    if owner is None or owner == "":
        return table
    return f"{_quote(assertIdentifier(owner, what="属主"), dialect)}.{table}"


def _quote(name: str, dialect: str) -> str:
    # 严格白名单里没有任何引号字符 ⇒ 无需转义，直接包起来即可。
    if dialect.lower() in _MYSQL_DIALECTS:
        return f"`{name}`"
    return f'"{name}"'


def _baseType(dataType: Any) -> str:
    """`VARCHAR2(100)` → `VARCHAR2`；`TIMESTAMP(6) WITH TIME ZONE` → `TIMESTAMP`。"""
    text = str(dataType or "").strip().upper()
    text = text.split("(", 1)[0].strip()
    return text.split(" ", 1)[0]


def _rowValue(rows: Sequence[Mapping[str, Any]], key: str) -> Any:
    if not rows:
        raise ProfileSqlError("计数查询没有返回任何行")
    normalized = {str(item).upper(): value for item, value in rows[0].items()}
    if key not in normalized:
        raise ProfileSqlError(f"计数结果缺少列 {key}")
    return normalized[key]


def _asInt(value: Any, what: str) -> int:
    if value is None:
        raise ProfileSqlError(f"{what} 为空（聚合结果不完整）")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileSqlError(f"{what} 不是整数：{value!r}") from exc
