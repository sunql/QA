"""规则推导引擎（feat-dq-rule-auto-generation spec §3/§5）：纯函数、无 IO。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.enums import DataType, DerivationType, ObjectType, RuleType, Severity
from app.domain.exceptions import ValidationError
from app.services.data_quality_evaluators._common import validate_identifier

_RULE_CODE_MAX = 100
_ALLOWED_VALUE_RE = re.compile(r"^[^']{1,50}$")
_TYPE_PATTERNS: dict[str, str] = {
    DataType.INT.value: r"^-?[0-9]+$",
    DataType.DECIMAL.value: r"^-?[0-9]+(\.[0-9]+)?$",
    DataType.DATETIME.value: r"^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{2}:[0-9]{2}(:[0-9]{2})?)?$",
}
_PHYSICAL_TEXT_FAMILY = ("char", "text")
_BOOLEAN_FLAGS = ("true", "false", "t", "f", "1", "0")
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
_THRESHOLD_SEVERITY: dict[RuleType, tuple[Decimal, Severity]] = {
    RuleType.COMPLETENESS: (Decimal("100"), Severity.HIGH),
    RuleType.UNIQUENESS: (Decimal("100"), Severity.HIGH),
}


@dataclass(frozen=True)
class ClassContext:
    class_id: int
    class_name: str
    source_table: str | None
    object_type: str | None

@dataclass(frozen=True)
class PropertyMeta:
    property_id: int
    property_name: str
    source_column: str | None
    data_type: str
    is_primary_key: bool
    is_foreign_key: bool
    ref_class: ClassContext | None = None
    allowed_values: list[str] | None = None


@dataclass(frozen=True)
class ColumnMeta:
    column_name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class SchemaIndex:
    tables: dict[str, dict[str, ColumnMeta]] = field(default_factory=dict)


@dataclass(frozen=True)
class JoinEdgeMeta:
    target_table: str
    source_columns: list[str]
    target_columns: list[str]
    target_date_columns: list[str]


@dataclass(frozen=True)
class RuleSuggestion:
    rule_code: str
    rule_name: str
    rule_type: RuleType
    target_table: str
    target_column: str | None
    rule_expression: str | None
    threshold: Decimal
    severity: Severity
    derivation_type: DerivationType
    source_property_id: int | None
    confidence: str
    reason: str


@dataclass(frozen=True)
class BlockedProperty:
    property_name: str
    reason: str


def buildRuleCode(className: str, propertyName: str, ruleType: RuleType) -> str:
    """DQ_<CLASS>_<PROP>_<TYPE>；超长截断 + sha256 短后缀，保证 pattern 与唯一性倾向。"""
    raw = f"DQ_{_slug(className)}_{_slug(propertyName)}_{ruleType.value}"
    if len(raw) <= _RULE_CODE_MAX:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:7]
    return f"{raw[:_RULE_CODE_MAX - 8]}_{digest}"


def _slug(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()
    return out or "X"


def deriveSuggestions(
    ctx: ClassContext,
    properties: list[PropertyMeta],
    joinEdges: list[JoinEdgeMeta],
    schemaIndex: SchemaIndex | None,
) -> tuple[list[RuleSuggestion], list[BlockedProperty]]:
    """按 spec §5 推导映射表产出建议；返回 (建议, 被阻断属性)。"""
    if ctx.source_table is None:
        return [], [BlockedProperty(p.property_name, "类未配置 source_table") for p in properties]
    validate_identifier(ctx.source_table, role="source_table")

    tableCols = schemaIndex.tables.get(ctx.source_table.upper()) if schemaIndex else None
    if tableCols is None:
        return [], [BlockedProperty(p.property_name, "数据源 schema 未缓存") for p in properties]

    suggestions: list[RuleSuggestion] = []
    blocked: list[BlockedProperty] = []
    for prop in properties:
        if prop.source_column is not None:
            validate_identifier(prop.source_column, role="source_column")
        col = _resolveColumn(tableCols, prop.source_column)
        if col is None:
            blocked.append(BlockedProperty(
                prop.property_name,
                "未配置物理列映射" if prop.source_column is None
                else f"物理列不存在: {prop.source_column}",
            ))
            continue
        validate_identifier(col.column_name, role="column")
        suggestions.extend(_deriveForProperty(ctx, prop, col))
    suggestions.extend(_deriveJoinConsistency(ctx, properties, joinEdges))
    return suggestions, blocked


def _resolveColumn(tableCols: dict[str, ColumnMeta], sourceColumn: str | None) -> ColumnMeta | None:
    if sourceColumn is None:
        return None
    return tableCols.get(sourceColumn.upper())


def _deriveForProperty(
    ctx: ClassContext, prop: PropertyMeta, col: ColumnMeta
) -> list[RuleSuggestion]:
    out: list[RuleSuggestion] = []
    column = col.column_name

    if prop.is_primary_key:
        out.append(_makeSuggestion(
            ctx, prop, RuleType.UNIQUENESS, DerivationType.PK_DERIVED,
            f"UNIQUE({column})", CONFIDENCE_HIGH, "主键唯一性",
        ))

    if not col.nullable:
        out.append(_makeSuggestion(
            ctx, prop, RuleType.COMPLETENESS, DerivationType.NOT_NULL,
            f"{column} IS NOT NULL", CONFIDENCE_HIGH, "非空约束",
        ))

    if prop.allowed_values:
        out.append(_deriveAllowedValues(ctx, prop, col))

    if prop.is_foreign_key:
        out.append(_deriveRef(ctx, prop, col))

    regex = _deriveTypeRegex(ctx, prop, col)
    if regex:
        out.append(regex)

    return out


def _deriveAllowedValues(
    ctx: ClassContext, prop: PropertyMeta, col: ColumnMeta
) -> RuleSuggestion:
    values = prop.allowed_values or []
    for value in values:
        if not _ALLOWED_VALUE_RE.match(value):
            raise ValidationError(
                f"allowed_values 含非法字符: {value!r}",
                detail="值域不允许包含单引号且长度需 1-50 字符",
            )
    quoted = ",".join(f"'{v}'" for v in values)
    return _makeSuggestion(
        ctx, prop, RuleType.VALIDITY, DerivationType.ALLOWED_VALUES,
        f"{col.column_name} IN ({quoted})", CONFIDENCE_HIGH, "固定值域",
    )


def _deriveRef(ctx: ClassContext, prop: PropertyMeta, col: ColumnMeta) -> RuleSuggestion:
    ref = prop.ref_class
    if ref is None:
        raise ValidationError("外键属性缺少 ref_class")
    if ref.source_table is None:
        raise ValidationError(f"参照类 {ref.class_name} 未配置 source_table")
    refTable = validate_identifier(ref.source_table, role="ref_table")
    refColumn = validate_identifier(prop.source_column or col.column_name, role="ref_column")
    if ref.object_type == ObjectType.REFERENCE.value:
        return _makeSuggestion(
            ctx, prop, RuleType.VALIDITY, DerivationType.DICT_REF,
            f"{col.column_name} IN (SELECT {refColumn} FROM {refTable})",
            CONFIDENCE_HIGH, "字典参照完整性",
        )
    return _makeSuggestion(
        ctx, prop, RuleType.REFERENTIAL, DerivationType.FK_DERIVED,
        f"REF {refTable}.{refColumn}", CONFIDENCE_HIGH, "外键参照",
    )


def _deriveTypeRegex(ctx: ClassContext, prop: PropertyMeta, col: ColumnMeta) -> RuleSuggestion | None:
    if not _isTextFamily(col.data_type):
        return None

    dataType = prop.data_type.upper()
    pattern = _TYPE_PATTERNS.get(dataType)
    if pattern:
        expr = f"{col.column_name} ~ '{pattern}'"
    elif dataType == DataType.BOOLEAN.value:
        flags = ",".join(f"'{f}'" for f in _BOOLEAN_FLAGS)
        expr = f"{col.column_name} IN ({flags})"
    else:
        return None

    if col.nullable:
        expr = f"{col.column_name} IS NULL OR {expr}"
    return _makeSuggestion(
        ctx, prop, RuleType.VALIDITY, DerivationType.LLM_DERIVED,
        expr, CONFIDENCE_MEDIUM, "类型不匹配正则校验",
    )


def _deriveJoinConsistency(
    ctx: ClassContext, properties: list[PropertyMeta], joinEdges: list[JoinEdgeMeta]
) -> list[RuleSuggestion]:
    out: list[RuleSuggestion] = []
    table = ctx.source_table or ""
    for edge in joinEdges:
        validate_identifier(edge.target_table, role="target_table")
        for col in edge.source_columns + edge.target_columns + edge.target_date_columns:
            validate_identifier(col, role="join_column")
        for prop in properties:
            if prop.source_column is None or prop.data_type.upper() != DataType.DATETIME.value:
                continue
            for targetDate in edge.target_date_columns:
                expr = _buildJoinExpression(table, prop.source_column, edge, targetDate)
                out.append(_makeSuggestion(
                    ctx, prop, RuleType.CONSISTENCY, DerivationType.JOIN_CONSISTENCY,
                    expr, CONFIDENCE_MEDIUM, "join 日期一致性",
                ))
    return out


def _buildJoinExpression(
    sourceTable: str, sourceDate: str, edge: JoinEdgeMeta, targetDate: str
) -> str:
    joinCond = " AND ".join(
        f"{edge.target_table}.{t} = {sourceTable}.{s}"
        for s, t in zip(edge.source_columns, edge.target_columns)
    )
    return (
        f"EXISTS (SELECT 1 FROM {edge.target_table} WHERE {joinCond} "
        f"AND {edge.target_table}.{targetDate} >= {sourceTable}.{sourceDate})"
    )


def _isTextFamily(dataType: str) -> bool:
    lower = dataType.lower()
    return any(family in lower for family in _PHYSICAL_TEXT_FAMILY)


def _makeSuggestion(
    ctx: ClassContext,
    prop: PropertyMeta,
    ruleType: RuleType,
    derivationType: DerivationType,
    expression: str,
    confidence: str,
    reason: str,
) -> RuleSuggestion:
    threshold, severity = _THRESHOLD_SEVERITY.get(
        ruleType, (Decimal("95"), Severity.MEDIUM)
    )
    return RuleSuggestion(
        rule_code=buildRuleCode(ctx.class_name, prop.property_name, ruleType),
        rule_name=f"{prop.property_name} {ruleType.value}",
        rule_type=ruleType,
        target_table=ctx.source_table or "",
        target_column=prop.source_column,
        rule_expression=expression,
        threshold=threshold,
        severity=severity,
        derivation_type=derivationType,
        source_property_id=prop.property_id,
        confidence=confidence,
        reason=reason,
    )
