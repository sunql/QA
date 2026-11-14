"""measure → SQL 编译器（P4b）。

只读结构化列，不嗅探 formula 文本。任一输入不足以确定性编译 ⇒ fail-closed，
上层降级 Tier B；绝不发半成品 SQL。
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.domain.exceptions import SqlSafetyError
from app.infrastructure.business_db_pool import _assert_read_only
from app.services.time_scope_renderer import (
    TimeScopeRenderUnsupportedError,
    renderTimeScopePredicate,
)

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期引入 ORM 依赖
    from app.domain.models import (
        OntologyClass,
        OntologyJoin,
        OntologyMetric,
        OntologyMetricSource,
    )

# ── 原因码（全部 fail-closed；字符串是跨层契约，勿随意改名）───────────────
COMPILED_DIALECT_UNSUPPORTED = "compiled_dialect_unsupported"
COMPILED_METRIC_TYPE_UNSUPPORTED = "compiled_metric_type_unsupported"
COMPILED_AGGREGATION_ORDER_UNSUPPORTED = "compiled_aggregation_order_unsupported"
COMPILED_MEASURE_MISSING = "compiled_measure_missing"
COMPILED_AGG_UNSUPPORTED = "compiled_agg_unsupported"
COMPILED_EXPR_MISSING = "compiled_expr_missing"
COMPILED_INVALID_FRAGMENT = "compiled_invalid_fragment"
COMPILED_MEASURE_CLASS_UNRESOLVED = "compiled_measure_class_unresolved"
COMPILED_CLASS_TABLE_MISSING = "compiled_class_table_missing"
COMPILED_JOIN_PATH_MISSING = "compiled_join_path_missing"
COMPILED_TIME_SCOPE_UNSUPPORTED = "compiled_time_scope_unsupported"
COMPILED_TIME_COLUMN_MISSING = "compiled_time_column_missing"
COMPILED_NOT_READ_ONLY = "compiled_not_read_only"

WARNING_GRAIN_KEYS_IGNORED = "grain_keys_ignored"

_SUPPORTED_DIALECTS = frozenset({"oracle", "postgresql", "mysql"})
_SUPPORTED_METRIC_TYPES = frozenset({"MEASURE", "RATIO", "DIFFERENCE"})
_SUPPORTED_JOIN_TYPES = frozenset({"INNER", "LEFT"})
_SUPPORTED_AGGS = frozenset({"SUM", "COUNT", "AVG", "COUNT_DISTINCT"})

_QUOTE = {"oracle": '"%s"', "postgresql": '"%s"', "mysql": "`%s`"}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMA_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DML_RE = re.compile(
    r"\b(?:insert|update|delete|drop|alter|truncate|grant|create)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompiledSql:
    """编译结果：ok=False 时 reason 必非空、sql 必为 None。"""

    ok: bool
    sql: str | None = None
    reason: str | None = None
    warnings: tuple[str, ...] = ()


def _fail(reason: str, warnings: tuple[str, ...] = ()) -> CompiledSql:
    return CompiledSql(ok=False, reason=reason, warnings=warnings)


def _normalizeToken(value: Any) -> str:
    """Enum / str 统一为大写 token；None → 空串。"""
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().upper()


def _isSafeIdentifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _isSafeTableName(value: Any) -> bool:
    """表名可为 schema-qualified（``THBI.PO_HEADER``）；每段都须是合法标识符。"""
    if not isinstance(value, str) or not value.strip():
        return False
    return all(_IDENTIFIER_RE.fullmatch(segment) for segment in value.split("."))


def _assertSafeFragment(fragment: Any) -> bool:
    """结构化片段守卫：拒绝注释/分号/多语句/不平衡括号引号/写操作关键词。"""
    if fragment is None:
        return True
    text = str(fragment).strip()
    if not text:
        return True
    if any(marker in text for marker in (";", "--", "/*", "*/")):
        return False
    if _DML_RE.search(text):
        return False
    return _isBalanced(text)


def _isBalanced(text: str) -> bool:
    stack: list[str] = []
    closing = {")": "(", "]": "["}
    quoteChar: str | None = None
    for char in text:
        if quoteChar is not None:
            if char == quoteChar:
                quoteChar = None
            continue
        if char in ("'", '"'):
            quoteChar = char
        elif char in ("(", "["):
            stack.append(char)
        elif char in closing and (not stack or stack.pop() != closing[char]):
            return False
    return quoteChar is None and not stack


def _renderExpr(expr: str, quote: str, qualifier: str | None = None) -> str:
    """裸标识符加方言引号（跨表时带表限定）；已守卫的算术表达式原样保留。"""
    if _IDENTIFIER_RE.fullmatch(expr):
        rendered = quote % expr
        return f"{qualifier}.{rendered}" if qualifier else rendered
    return expr


def _measureParts(measure: Any) -> tuple[str, str | None, str | None] | None:
    """抽取 (agg, expr, filter)；非法 agg 返回 None。"""
    if not isinstance(measure, dict):
        return None
    agg = _normalizeToken(measure.get("agg"))
    if agg not in _SUPPORTED_AGGS:
        return None
    exprRaw = measure.get("expr")
    filterRaw = measure.get("filter")
    expr = str(exprRaw).strip() if exprRaw is not None else None
    filterText = str(filterRaw).strip() if filterRaw is not None else None
    return agg, expr or None, filterText or None


def _renderAggregate(
    measure: Any, quote: str, qualifier: str | None = None
) -> tuple[str | None, str | None]:
    """度量 → 聚合表达式（measure.filter 走 CASE WHEN，不进 WHERE）。"""
    if not isinstance(measure, dict):
        return None, COMPILED_MEASURE_MISSING
    parts = _measureParts(measure)
    if parts is None:
        return None, COMPILED_AGG_UNSUPPORTED
    agg, expr, filterText = parts
    if not _assertSafeFragment(expr) or not _assertSafeFragment(filterText):
        return None, COMPILED_INVALID_FRAGMENT

    if agg == "COUNT" and expr is None:
        if filterText:
            return f"COUNT(CASE WHEN {filterText} THEN 1 END)", None
        return "COUNT(*)", None
    if expr is None:
        return None, COMPILED_EXPR_MISSING

    rendered = _renderExpr(expr, quote, qualifier)
    if filterText:
        rendered = f"CASE WHEN {filterText} THEN {rendered} END"
    if agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {rendered})", None
    return f"{agg}({rendered})", None


def _renderPerRow(
    measure: Any, quote: str, qualifier: str | None = None
) -> tuple[str | None, str | None]:
    """AVG_OF_RATIOS 用：逐行表达式（忽略度量自身聚合）。"""
    if not isinstance(measure, dict):
        return None, COMPILED_MEASURE_MISSING
    parts = _measureParts(measure)
    if parts is None:
        return None, COMPILED_AGG_UNSUPPORTED
    agg, expr, filterText = parts
    if not _assertSafeFragment(expr) or not _assertSafeFragment(filterText):
        return None, COMPILED_INVALID_FRAGMENT

    if expr is None:
        if agg != "COUNT":
            return None, COMPILED_EXPR_MISSING
        rendered = "1"
    else:
        rendered = _renderExpr(expr, quote, qualifier)
    if filterText:
        rendered = f"CASE WHEN {filterText} THEN {rendered} END"
    return rendered, None


def _resolveMeasureClassId(
    measure: dict,
    metric: OntologyMetric,
    sources: Sequence[OntologyMetricSource],
) -> int | None:
    """measure.class_id → 唯一源类 → metric.target_class_id → None。"""
    explicit = measure.get("class_id")
    if explicit is not None:
        return explicit
    sourceClassIds = {source.class_id for source in sources}
    if len(sourceClassIds) == 1:
        return next(iter(sourceClassIds))
    if metric.target_class_id is not None:
        return metric.target_class_id
    return None


def _classTable(
    classId: int | None, classById: dict[int, OntologyClass]
) -> tuple[str | None, str | None]:
    if classId is None or classId not in classById:
        return None, COMPILED_MEASURE_CLASS_UNRESOLVED
    table = classById[classId].source_table
    if table is None or not str(table).strip():
        return None, COMPILED_CLASS_TABLE_MISSING
    tableText = str(table).strip()
    if not _isSafeTableName(tableText):
        return None, COMPILED_INVALID_FRAGMENT
    return tableText, None


def _resolveSchemaPrefix(schemaPrefix: Any) -> tuple[str | None, str | None]:
    if schemaPrefix is None:
        return None, None
    if not isinstance(schemaPrefix, str) or not _SCHEMA_PREFIX_RE.fullmatch(schemaPrefix):
        return None, COMPILED_INVALID_FRAGMENT
    return schemaPrefix, None


def _bakeTableName(table: str, schemaPrefix: str | None) -> str:
    if schemaPrefix and not table.startswith(f"{schemaPrefix}."):
        return f"{schemaPrefix}.{table}"
    return table


def _quoteTable(bakedTable: str, quote: str) -> str:
    return ".".join(quote % part for part in bakedTable.split("."))


def _findJoinPath(
    startId: int, targetId: int, joins: Sequence[OntologyJoin]
) -> list[tuple[int, int, OntologyJoin]] | None:
    """无向图上 BFS 最短路径；返回 [(fromClassId, toClassId, join)]。"""
    if startId == targetId:
        return []
    adjacency: dict[int, list[tuple[int, OntologyJoin]]] = {}
    for join in joins:
        adjacency.setdefault(join.source_class_id, []).append((join.target_class_id, join))
        adjacency.setdefault(join.target_class_id, []).append((join.source_class_id, join))

    parents: dict[int, tuple[int, OntologyJoin] | None] = {startId: None}
    queue: deque[int] = deque([startId])
    while queue:
        node = queue.popleft()
        for neighbour, join in adjacency.get(node, ()):
            if neighbour in parents:
                continue
            parents[neighbour] = (node, join)
            if neighbour == targetId:
                return _reconstructPath(parents, targetId)
            queue.append(neighbour)
    return None


def _reconstructPath(
    parents: dict[int, tuple[int, OntologyJoin] | None], targetId: int
) -> list[tuple[int, int, OntologyJoin]]:
    edges: list[tuple[int, int, OntologyJoin]] = []
    current = targetId
    while parents[current] is not None:
        previous, join = parents[current]
        edges.append((previous, current, join))
        current = previous
    edges.reverse()
    return edges


def _renderJoinStep(
    toClassId: int,
    join: OntologyJoin,
    classById: dict[int, OntologyClass],
    quote: str,
) -> tuple[str | None, str | None]:
    joinType = _normalizeToken(join.join_type) or "INNER"
    if joinType not in _SUPPORTED_JOIN_TYPES:
        return None, COMPILED_JOIN_PATH_MISSING

    sourceColumns = list(join.source_columns or [])
    targetColumns = list(join.target_columns or [])
    if not sourceColumns or len(sourceColumns) != len(targetColumns):
        return None, COMPILED_JOIN_PATH_MISSING

    sourceTable, reason = _classTable(join.source_class_id, classById)
    if reason is not None:
        return None, reason
    targetTable, reason = _classTable(join.target_class_id, classById)
    if reason is not None:
        return None, reason
    joinedTable, reason = _classTable(toClassId, classById)
    if reason is not None:
        return None, reason

    conditions: list[str] = []
    for sourceColumn, targetColumn in zip(sourceColumns, targetColumns, strict=True):
        if not _isSafeIdentifier(sourceColumn) or not _isSafeIdentifier(targetColumn):
            return None, COMPILED_INVALID_FRAGMENT
        conditions.append(
            f"{_quoteTable(sourceTable, quote)}.{quote % sourceColumn} = "
            f"{_quoteTable(targetTable, quote)}.{quote % targetColumn}"
        )
    return f"{joinType} JOIN {_quoteTable(joinedTable, quote)} ON " + " AND ".join(
        conditions
    ), None


def _renderJoinClauses(
    fromClassId: int,
    requiredClassIds: Sequence[int],
    joins: Sequence[OntologyJoin],
    classById: dict[int, OntologyClass],
    quote: str,
) -> tuple[list[str] | None, str | None]:
    clauses: list[str] = []
    usedJoinIds: set[int] = set()
    for targetId in requiredClassIds:
        path = _findJoinPath(fromClassId, targetId, joins)
        if path is None:
            return None, COMPILED_JOIN_PATH_MISSING
        for _fromId, toId, join in path:
            if id(join) in usedJoinIds:
                continue
            clause, reason = _renderJoinStep(toId, join, classById, quote)
            if reason is not None:
                return None, reason
            usedJoinIds.add(id(join))
            clauses.append(clause)
    return clauses, None


def _renderWhere(
    metric: OntologyMetric,
    *,
    dialect: str,
    quote: str,
    timeScope: Any | None,
) -> tuple[str | None, str | None]:
    predicates: list[str] = []

    globalFilter = metric.filter_expression
    if globalFilter is not None and str(globalFilter).strip():
        text = str(globalFilter).strip()
        if not _assertSafeFragment(text):
            return None, COMPILED_INVALID_FRAGMENT
        predicates.append(text)

    if timeScope is not None:
        timeColumn = metric.time_column
        if not _isSafeIdentifier(timeColumn):
            return None, COMPILED_TIME_COLUMN_MISSING
        try:
            predicate = renderTimeScopePredicate(timeScope, quote % timeColumn, dialect)
        except TimeScopeRenderUnsupportedError:
            return None, COMPILED_TIME_SCOPE_UNSUPPORTED
        predicates.append(predicate)

    if not predicates:
        return "", None
    if len(predicates) == 1:
        return f" WHERE {predicates[0]}", None
    # 多谓词必须各自加括号：否则 `A OR B AND time` 会让 OR 分支绕过时间过滤。
    return " WHERE " + " AND ".join(f"({predicate})" for predicate in predicates), None


def _renderValueExpr(
    metricType: str,
    numerator: dict,
    denominator: dict | None,
    quote: str,
    aggregationOrder: Any,
    numeratorQualifier: str | None = None,
    denominatorQualifier: str | None = None,
) -> tuple[str | None, str | None]:
    if metricType == "MEASURE":
        return _renderAggregate(numerator, quote, numeratorQualifier)
    if denominator is None:
        return None, COMPILED_MEASURE_MISSING

    if metricType == "DIFFERENCE":
        numeratorExpr, reason = _renderAggregate(numerator, quote, numeratorQualifier)
        if reason is not None:
            return None, reason
        denominatorExpr, reason = _renderAggregate(denominator, quote, denominatorQualifier)
        if reason is not None:
            return None, reason
        return f"{numeratorExpr} - {denominatorExpr}", None

    order = _normalizeToken(aggregationOrder) or "WEIGHTED_RATIO"
    if order == "WEIGHTED_RATIO":
        numeratorExpr, reason = _renderAggregate(numerator, quote, numeratorQualifier)
        if reason is not None:
            return None, reason
        denominatorExpr, reason = _renderAggregate(denominator, quote, denominatorQualifier)
        if reason is not None:
            return None, reason
        return f"{numeratorExpr} / NULLIF({denominatorExpr}, 0)", None
    if order == "AVG_OF_RATIOS":
        numeratorExpr, reason = _renderPerRow(numerator, quote, numeratorQualifier)
        if reason is not None:
            return None, reason
        denominatorExpr, reason = _renderPerRow(denominator, quote, denominatorQualifier)
        if reason is not None:
            return None, reason
        return f"AVG({numeratorExpr} / NULLIF({denominatorExpr}, 0))", None
    return None, COMPILED_AGGREGATION_ORDER_UNSUPPORTED


def compileMetricSql(
    metric: OntologyMetric,
    *,
    sources: Sequence[OntologyMetricSource],
    classes: Sequence[OntologyClass],
    joins: Sequence[OntologyJoin],
    datasourceType: str,
    schemaPrefix: str | None = None,
    timeScope: Any | None = None,
) -> CompiledSql:
    """结构化指标定义 → 单值只读 SELECT；不足以确定性编译时 fail-closed。"""
    dialect = _normalizeToken(datasourceType).lower()
    if dialect not in _SUPPORTED_DIALECTS:
        return _fail(COMPILED_DIALECT_UNSUPPORTED)

    warnings = (WARNING_GRAIN_KEYS_IGNORED,) if metric.grain_keys else ()
    metricType = _normalizeToken(metric.metric_type)
    if metricType not in _SUPPORTED_METRIC_TYPES:
        return _fail(COMPILED_METRIC_TYPE_UNSUPPORTED, warnings)

    safePrefix, reason = _resolveSchemaPrefix(schemaPrefix)
    if reason is not None:
        return _fail(reason, warnings)

    quote = _QUOTE[dialect]
    numerator = metric.numerator_measure
    if not isinstance(numerator, dict):
        return _fail(COMPILED_MEASURE_MISSING, warnings)
    numeratorClassId = _resolveMeasureClassId(numerator, metric, sources)
    if numeratorClassId is None:
        return _fail(COMPILED_MEASURE_CLASS_UNRESOLVED, warnings)

    classById = {ontologyClass.id: ontologyClass for ontologyClass in classes}
    numeratorTable, reason = _classTable(numeratorClassId, classById)
    if reason is not None:
        return _fail(reason, warnings)

    denominator: dict | None = None
    denominatorClassId: int | None = None
    denominatorTable: str | None = None
    if metricType in {"RATIO", "DIFFERENCE"}:
        rawDenominator = metric.denominator_measure
        if not isinstance(rawDenominator, dict):
            return _fail(COMPILED_MEASURE_MISSING, warnings)
        denominator = rawDenominator
        denominatorClassId = _resolveMeasureClassId(denominator, metric, sources)
        if denominatorClassId is None:
            return _fail(COMPILED_MEASURE_CLASS_UNRESOLVED, warnings)
        denominatorTable, reason = _classTable(denominatorClassId, classById)
        if reason is not None:
            return _fail(reason, warnings)

    # 跨表 SELECT 必须限定值列，且按各度量自身类表限定（不是统一用 FROM 表）。
    multiTable = denominatorTable is not None and denominatorTable != numeratorTable
    numeratorQualifier = (
        _quoteTable(_bakeTableName(numeratorTable, safePrefix), quote) if multiTable else None
    )
    denominatorQualifier = (
        _quoteTable(_bakeTableName(denominatorTable, safePrefix), quote)
        if multiTable and denominatorTable is not None
        else None
    )

    valueExpr, reason = _renderValueExpr(
        metricType,
        numerator,
        denominator,
        quote,
        metric.aggregation_order,
        numeratorQualifier,
        denominatorQualifier,
    )
    if reason is not None:
        return _fail(reason, warnings)

    requiredClassIds: list[int] = []
    if denominatorClassId is not None and denominatorClassId != numeratorClassId:
        requiredClassIds.append(denominatorClassId)

    joinClauses, reason = _renderJoinClauses(
        numeratorClassId, requiredClassIds, joins, classById, quote
    )
    if reason is not None:
        return _fail(reason, warnings)

    whereClause, reason = _renderWhere(
        metric, dialect=dialect, quote=quote, timeScope=timeScope
    )
    if reason is not None:
        return _fail(reason, warnings)

    fromTable = _quoteTable(_bakeTableName(numeratorTable, safePrefix), quote)
    sql = f"SELECT {valueExpr} AS value FROM {fromTable}"
    if joinClauses:
        sql += " " + " ".join(joinClauses)
    sql += whereClause

    try:
        _assert_read_only(sql)
    except SqlSafetyError:
        return _fail(COMPILED_NOT_READ_ONLY, warnings)
    return CompiledSql(ok=True, sql=sql, warnings=warnings)
