"""formula_parser（Phase 2.2）。

从 SQL formula 文本中提取：
1. 列引用（alias.column 或 column 形式）
2. 聚合函数名（用于推断 lineage 边目标层 KPI / DWS 等）

实现说明：
- 不引入 SQL 解析器（sqlparse 体积过大；formula 通常简短且模式固定）
- 用 token 流扫描 + 关键词识别，覆盖 SUM/AVG/COUNT/MAX/MIN + 表别名 + 反引号/双引号
- 不支持完整 SQL 语法（无 JOIN / 无 GROUP BY 等嵌套子句），仅识别列引用与聚合函数名
- best-effort：解析失败返回空 ParsedFormula（caller 应记录 warning）
"""

from __future__ import annotations

from dataclasses import dataclass
import re


# SQL 关键字黑名单（不应被误识别为裸列名）
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE",
        "CASE", "WHEN", "THEN", "ELSE", "END",
        "AS", "ON", "IN", "EXISTS", "BETWEEN", "LIKE",
        "DISTINCT", "ALL", "ANY", "SOME",
        "PARTITION", "BY", "ORDER", "OVER", "ASC", "DESC",
        "IF", "COALESCE", "NULLIF", "CAST", "CONVERT",
    }
)

# 聚合函数名（按 SQL 标准 + 主流方言扩展）
_AGGREGATE_FUNCTIONS: frozenset[str] = frozenset(
    {"SUM", "AVG", "COUNT", "MAX", "MIN"}
)

# 列引用正则：
#   形式 1（首选）：alias.column
#   形式 2：裸 column（无表别名）— 表别名为空字符串
# 标识符允许反引号/双引号包裹
#   group("alias") = 别名（可空字符串）
#   group("col") = 列名（必填）
_COLUMN_REF_RE = re.compile(
    r"""
    (?:
        # alias.column
        (?P<alias1>[A-Za-z_][A-Za-z0-9_]*|`[^`]+`|"[^"]+")
        \.
        (?P<col1>[A-Za-z_][A-Za-z0-9_]*|`[^`]+`|"[^"]+")
    )
    |
    (?:
        # 裸 column（无前缀点号；要求不在 SQL 关键字列表中以减少误识别）
        (?<![A-Za-z0-9_.`"])
        (?P<col2>[A-Za-z_][A-Za-z0-9_]*)
        (?![A-Za-z0-9_`"])
    )
    """,
    re.VERBOSE,
)

# 聚合函数正则：捕获函数名
_AGG_FN_RE = re.compile(r"\b(SUM|AVG|COUNT|MAX|MIN)\s*\(", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedFormula:
    """formula 解析结果（不可变 dataclass）。

    column_refs: (alias, column) 元组列表；alias 可为空字符串表示无表别名
    aggregate_functions: 出现的聚合函数名（去重保序）
    """

    column_refs: tuple[tuple[str, str], ...]
    aggregate_functions: tuple[str, ...]


def _stripQuotes(identifier: str) -> str:
    """去除反引号/双引号包裹的标识符。"""
    if len(identifier) >= 2 and identifier[0] == identifier[-1] and identifier[0] in {'`', '"'}:
        return identifier[1:-1]
    return identifier


def parseFormula(formula: str | None) -> ParsedFormula:
    """解析 SQL formula 文本。

    Args:
        formula: SQL 表达式（如 "SUM(t.ORDER_QTY) / NULLIF(COUNT(t.ID), 0)"）。
                 None 或空字符串返回空 ParsedFormula。

    Returns:
        ParsedFormula：不可变 dataclass，含 column_refs 与 aggregate_functions。

    Notes:
        best-effort：解析失败（如未闭合括号）不抛错，返回尽可能多的解析结果。
    """
    if not formula or not formula.strip():
        return ParsedFormula(column_refs=(), aggregate_functions=())

    text = formula

    # 1. 提取聚合函数（去重保序）
    agg_names: list[str] = []
    seen: set[str] = set()
    for match in _AGG_FN_RE.finditer(text):
        name = match.group(1).upper()
        if name not in seen:
            seen.add(name)
            agg_names.append(name)

    # 2. 提取列引用
    column_refs: list[tuple[str, str]] = []
    seen_cols: set[tuple[str, str]] = set()
    for match in _COLUMN_REF_RE.finditer(text):
        alias_raw = match.group("alias1")
        col_raw = match.group("col1") or match.group("col2")
        if col_raw is None:
            continue
        # alias1 形式：alias.column
        if alias_raw is not None:
            alias = _stripQuotes(alias_raw)
            col = _stripQuotes(col_raw)
        else:
            # 裸 column：alias 留空
            alias = ""
            col = col_raw
        # 列名必须非空（防御性检查）
        if not col:
            continue
        # 过滤 SQL 关键字误识别（bare column 路径可能命中 SUM/COUNT/CASE 等）
        if alias == "" and (col.upper() in _AGGREGATE_FUNCTIONS or col.upper() in _SQL_KEYWORDS):
            continue
        key = (alias, col)
        if key not in seen_cols:
            seen_cols.add(key)
            column_refs.append(key)

    return ParsedFormula(
        column_refs=tuple(column_refs),
        aggregate_functions=tuple(agg_names),
    )