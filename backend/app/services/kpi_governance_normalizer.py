"""KPI 治理结构化字段规范化（P0 止血，纯函数、无 IO）。

背景：``kpi_catalog`` 的结构化列（numerator_columns / denominator_columns /
grain_keys）长期与 ``ontology_metric`` 的技术形态对不上，层 2 对账判
mismatch ⇒ 契约注入把 KPI 整条丢掉。本模块把治理侧**对齐到技术侧**：

- 已有 measure ⇒ 治理列 := measure ``expr`` 的列集合；
- MEASURE 缺 measure ⇒ 从 ``formula`` 外层聚合派生；
- RATIO 缺两侧 measure ⇒ 按顶层唯一 ``/`` 拆分派生；
- ``execution_source=NONE`` ⇒ 结构化字段清空（unknown / Tier C），业务文本不动。

设计约束：
- **不重写对账**：判定一律走 ``governanceSpecOf`` / ``techniqueOf`` / ``reconcileMetric``；
- **拒绝猜**：派生列必须出现在该 metric 的源类物理列里，否则抛
  :class:`NormalizationError`（CLI 退出码 2）。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_metric_reconcile import (
    GovernanceSpec,
    MeasureSpec,
    MetricTechnique,
    reconcileMetric,
)
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
from app.services.nl2sql_semantic_guard import measureColumnIdentifiers

_STRING_LITERAL = r"'(?:[^']|'')*'"
# 别名前缀与字符串字面量二选一：字面量整体保留（`'N.A'` 不是别名）。
_ALIAS_OR_LITERAL = re.compile(rf"{_STRING_LITERAL}|\b[A-Za-z_][A-Za-z0-9_]*\.")
_SELECT_PREFIX = re.compile(r"SELECT\b", re.IGNORECASE)
_FUNCTION_HEAD = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# 空值守卫包装：`NULLIF(SUM(x), 0)` 的度量是里面的 SUM，不是 NULLIF 本身。
_WRAPPER_FUNCTIONS = frozenset({"NULLIF", "COALESCE", "NVL", "IFNULL"})

BASIS_MEASURE_JSON = "measure_json"
BASIS_FORMULA_MEASURE = "formula_split_measure"
BASIS_FORMULA_RATIO = "formula_split_ratio"
BASIS_NONE_TIER_C = "none_tier_c"

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"


class NormalizationError(RuntimeError):
    """派生结果不可信（缺列、formula 不可解析、KPI 未挂靠）⇒ 拒绝写库。"""


@dataclass(frozen=True)
class MeasurePlan:
    """可直接落库的 measure JSON（只保留 agg/expr）。"""

    agg: str
    expr: str | None = None

    def toRaw(self) -> dict[str, str]:
        raw = {"agg": self.agg}
        if self.expr is not None:
            raw["expr"] = self.expr
        return raw


@dataclass(frozen=True)
class NormalizationPlan:
    kpi_code: str
    metric_id: int | None
    tier: str
    basis: str
    numerator_columns: tuple[str, ...]
    denominator_columns: tuple[str, ...]
    grain_keys: tuple[str, ...]
    numerator_measure: dict[str, Any] | None
    denominator_measure: dict[str, Any] | None
    before: dict[str, Any]
    business_todo: str | None = None
    verdict_before: str = ""
    verdict_after: str = ""
    is_noop: bool = False
    findings_after: tuple[str, ...] = field(default=())

    @property
    def tier_after(self) -> str:
        return self.tier


def _tokens(values: Iterable[str] | None) -> tuple[str, ...]:
    """规范化列名：去空白、转大写、去重保序（与对账的大小写不敏感一致）。"""
    seen: dict[str, None] = {}
    for value in values or ():
        token = str(value).strip().upper()
        if token:
            seen.setdefault(token, None)
    return tuple(seen)


def _measureColumns(measure: MeasureSpec | None) -> tuple[str, ...]:
    if measure is None:
        return ()
    return tuple(sorted(col.upper() for col in measure.columns()))


def stripAliases(expr: str) -> str:
    """去掉 ``R.COL`` / ``THBI.TABLE`` 这类前缀；字符串字面量原样保留。"""

    def _replace(match: re.Match[str]) -> str:
        text = match.group(0)
        return text if text.startswith("'") else ""

    return _ALIAS_OR_LITERAL.sub(_replace, expr)


def _scanOutsideQuotes(text: str) -> Iterator[tuple[int, str, int]]:
    """产出字面量之外的 ``(index, char, depth)``；depth 是该字符**之前**的括号深度。

    SQL 转义写法 ``''``：连续两个引号仍在字面量内，必须一起跳过。
    """
    depth = 0
    inQuote = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if inQuote and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            inQuote = not inQuote
            index += 1
            continue
        if not inQuote:
            yield index, char, depth
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        index += 1


def splitTopLevelDivision(expr: str) -> tuple[str, str] | None:
    """只在括号深度 0 且**恰好一个** ``/`` 时拆分；否则返回 None。

    字符串字面量里的 ``/``（如 ``'A/B'``）不是除法，扫描时跳过。
    """
    parts: list[str] = []
    start = 0
    for index, char, depth in _scanOutsideQuotes(expr):
        if char == "/" and depth == 0:
            parts.append(expr[start:index])
            start = index + 1
    if len(parts) != 1:
        return None
    parts.append(expr[start:])
    return parts[0].strip(), parts[1].strip()


def _selectItem(formula: str) -> str | None:
    """取 SELECT 与**顶层** FROM 之间的表达式；括号/字面量里的 FROM 不算。"""
    text = (formula or "").strip()
    match = _SELECT_PREFIX.match(text)
    if match is None:
        return None
    for index, _char, depth in _scanOutsideQuotes(text):
        if depth != 0 or index < match.end():
            continue
        if text[index:index + 4].upper() != "FROM":
            continue
        before = text[index - 1] if index > 0 else " "
        after = text[index + 4] if index + 4 < len(text) else " "
        if before.isalnum() or before == "_" or after.isalnum() or after == "_":
            continue
        item = text[match.end():index].strip()
        return item or None
    return None


def _parseAggCall(text: str) -> tuple[str, str] | None:
    """``AGG(...)`` → (AGG, 参数体)；只认「整段就是一次调用」的形式。"""
    match = _FUNCTION_HEAD.match(text)
    if match is None:
        return None
    openIndex = match.end() - 1
    for index, char, depth in _scanOutsideQuotes(text):
        if index < openIndex or char != ")" or depth != 1:
            continue
        if text[index + 1:].strip():
            return None
        return match.group(1).upper(), text[openIndex + 1:index]
    return None


def _firstArgument(inner: str) -> str | None:
    """取第一个顶层逗号之前的参数（``NULLIF(SUM(x), 0)`` → ``SUM(x)``）。"""
    for index, char, depth in _scanOutsideQuotes(inner):
        if char == "," and depth == 0:
            return inner[:index].strip() or None
    return inner.strip() or None


def _parseAggSide(side: str) -> tuple[str, str] | None:
    """一侧的聚合：``SUM(x)`` 直接取；``NULLIF(SUM(x), 0)`` 取里面的聚合。"""
    parsed = _parseAggCall(side.strip())
    if parsed is None:
        return None
    name, inner = parsed
    if name not in _WRAPPER_FUNCTIONS:
        return parsed
    first = _firstArgument(inner)
    if first is None:
        return None
    return _parseAggCall(first)


def _normalizeExpr(expr: str) -> str:
    """派生表达式只去首尾空白：**保留**别名限定与 ``TRUNC`` 等语义。

    2026-11-11 复审实测：去掉 ``O.``/``P.`` 会把 ``O.UNIT_PRICE - P.UNIT_PRICE``
    压成 ``UNIT_PRICE - UNIT_PRICE``（恒 0）；去掉 TRUNC 会改变取整语义。
    列集合由 :func:`_columnTokens` 单独净化，不靠改写表达式。
    """
    return expr.strip()


def _columnTokens(expression: str) -> set[str]:
    """物理列候选：去别名前缀、屏蔽字面量、剔除函数名（见
    :func:`measureColumnIdentifiers`）。只做**严格子集**校验（不猜列名）。
    """
    return measureColumnIdentifiers(expression)


def _assertPhysical(
    expression: str, allowedColumns: frozenset[str], kpiCode: str
) -> None:
    """派生表达式的列必须能在源类物理列里找到，否则拒绝（不猜）。"""
    tokens = _columnTokens(expression)
    missing = sorted(token for token in tokens if token not in allowedColumns)
    if missing:
        raise NormalizationError(
            f"{kpiCode}: 派生度量的列不在源类物理列中：expr={expression!r} "
            f"缺失={missing}"
        )


def _existingPlan(measure: MeasureSpec | None) -> MeasurePlan | None:
    if measure is None:
        return None
    return MeasurePlan(agg=measure.agg, expr=measure.expr)


def _deriveRatioPlans(
    kpiCode: str,
    item: str,
    numerator: MeasureSpec | None,
    denominator: MeasureSpec | None,
) -> tuple[MeasurePlan | None, MeasurePlan | None]:
    """RATIO 缺 measure 的两侧派生。

    两种合法形态（**只写缺的那一侧**：已有 measure 可能带 filter/class_id，
    重写会丢语义）：

    1. 两侧聚合相除：``SUM(num) / NULLIF(SUM(den), 0)``；
    2. 单个外层聚合包住除法：``AVG((num - den) / NULLIF(den, 0))``。
    """
    split = splitTopLevelDivision(item)
    if split is not None:
        leftSide = _parseAggSide(split[0])
        rightSide = _parseAggSide(split[1])
        if numerator is None and leftSide is None:
            raise NormalizationError(
                f"{kpiCode}: RATIO formula 的分子侧不是可识别聚合：{split[0]!r}"
            )
        if denominator is None and rightSide is None:
            raise NormalizationError(
                f"{kpiCode}: RATIO formula 的分母侧不是可识别聚合：{split[1]!r}"
            )
        numPlan = _existingPlan(numerator)
        denPlan = _existingPlan(denominator)
        if numPlan is None and leftSide is not None:
            numPlan = MeasurePlan(agg=leftSide[0], expr=_normalizeExpr(leftSide[1]))
        if denPlan is None and rightSide is not None:
            denPlan = MeasurePlan(agg=rightSide[0], expr=_normalizeExpr(rightSide[1]))
        return numPlan, denPlan

    outer = _parseAggCall(item)
    if outer is None:
        raise NormalizationError(
            f"{kpiCode}: RATIO 缺 measure，且 formula 不是可识别的聚合表达式：{item!r}"
        )
    agg, body = outer[0], _normalizeExpr(outer[1])
    inner = splitTopLevelDivision(body)
    if inner is None:
        raise NormalizationError(
            f"{kpiCode}: RATIO 缺 measure，且 formula 参数体没有唯一的顶层 '/'"
        )
    left, right = inner
    numPlan = _existingPlan(numerator) or MeasurePlan(agg=agg, expr=left)
    denPlan = _existingPlan(denominator) or MeasurePlan(agg=agg, expr=right)
    return numPlan, denPlan


def _deriveMeasures(
    kpiCode: str,
    metric: OntologyMetric,
) -> tuple[MeasurePlan | None, MeasurePlan | None, bool, bool, str]:
    """返回 (分子度量, 分母度量, 是否写分子, 是否写分母, basis)。

    ``是否写`` 为 False 表示沿用原 measure JSON（不能丢 filter/class_id 等键）。
    """
    numerator = MeasureSpec.fromRaw(metric.numerator_measure)
    denominator = MeasureSpec.fromRaw(metric.denominator_measure)
    if numerator is not None and denominator is not None:
        return (
            _existingPlan(numerator),
            _existingPlan(denominator),
            False,
            False,
            BASIS_MEASURE_JSON,
        )

    if metric.metric_type == "MEASURE" and numerator is not None:
        # 分子已就绪（分母对 MEASURE 无意义）⇒ 无需派生，也不写。
        return (
            _existingPlan(numerator),
            _existingPlan(denominator),
            False,
            False,
            BASIS_MEASURE_JSON,
        )

    item = _selectItem(metric.formula or "")
    if item is None:
        raise NormalizationError(
            f"{kpiCode}: metric {metric.id} 缺 measure 且 formula 无法解析 SELECT 项"
        )

    if metric.metric_type == "MEASURE":
        parsed = _parseAggCall(item)
        if parsed is None:
            raise NormalizationError(
                f"{kpiCode}: MEASURE formula 不是单个外层聚合：{item!r}"
            )
        agg, body = parsed[0], _normalizeExpr(parsed[1])
        return (
            MeasurePlan(agg=agg, expr=body),
            _existingPlan(denominator),
            True,
            False,
            BASIS_FORMULA_MEASURE,
        )

    if metric.metric_type == "RATIO" and (numerator is None or denominator is None):
        numPlan, denPlan = _deriveRatioPlans(kpiCode, item, numerator, denominator)
        return (
            numPlan,
            denPlan,
            numerator is None,
            denominator is None,
            BASIS_FORMULA_RATIO,
        )

    raise NormalizationError(
        f"{kpiCode}: metric {metric.id}（{metric.metric_type}）的 measure 组合"
        "无法用现有规则派生"
    )


def _tierOf(result: Any) -> str:
    """Tier A = 层 2 pass 且层 1/3 无降级；pass 但降级 = B；其余 = C。"""
    if result.isTierAEligible:
        return TIER_A
    if result.isPass:
        return TIER_B
    return TIER_C


def _governanceOf(
    numerator: Sequence[str],
    denominator: Sequence[str],
    grain: Sequence[str],
) -> GovernanceSpec:
    return GovernanceSpec(
        numerator_columns=tuple(numerator),
        denominator_columns=tuple(denominator),
        grain_keys=tuple(grain),
    )


def _techniqueWithMeasures(
    metric: OntologyMetric,
    numerator: MeasureSpec | None,
    denominator: MeasureSpec | None,
) -> MetricTechnique:
    """在原技术形态上替换两侧 measure；其余字段（formula/grain…）原样。"""
    base = techniqueOf(metric)
    return MetricTechnique(
        metric_type=base.metric_type,
        numerator=numerator if numerator is not None else base.numerator,
        denominator=denominator if denominator is not None else base.denominator,
        aggregation_order=base.aggregation_order,
        grain_keys=base.grain_keys,
        filter_expression=base.filter_expression,
        formula=base.formula,
        execution_source=base.execution_source,
    )


def _withAfter(
    plan: NormalizationPlan, result: Any, *, is_noop: bool
) -> NormalizationPlan:
    findings = tuple(
        f"{finding.layer}/{finding.reason}" for finding in result.findings
    )
    return NormalizationPlan(
        kpi_code=plan.kpi_code,
        metric_id=plan.metric_id,
        tier=_tierOf(result),
        basis=plan.basis,
        numerator_columns=plan.numerator_columns,
        denominator_columns=plan.denominator_columns,
        grain_keys=plan.grain_keys,
        numerator_measure=plan.numerator_measure,
        denominator_measure=plan.denominator_measure,
        before=plan.before,
        business_todo=plan.business_todo,
        verdict_before=plan.verdict_before,
        verdict_after=result.verdict,
        is_noop=is_noop,
        findings_after=findings,
    )


def _isNoop(kpi: KpiCatalog, plan: NormalizationPlan) -> bool:
    """目标值 == 现值且无需补 measure ⇒ 重跑不产生任何写入。

    列集合按**无序**比较：治理侧列序不参与任何判定，仅顺序不同时重写
    属于无意义变更（apply 应保持幂等）。
    """
    return (
        sorted(_tokens(kpi.numerator_columns)) == sorted(plan.numerator_columns)
        and sorted(_tokens(kpi.denominator_columns))
        == sorted(plan.denominator_columns)
        and sorted(_tokens(kpi.grain_keys)) == sorted(plan.grain_keys)
        and plan.numerator_measure is None
        and plan.denominator_measure is None
    )


def deriveNormalization(
    kpi: KpiCatalog,
    metric: OntologyMetric | None,
    class_columns: frozenset[str],
) -> NormalizationPlan:
    """单条 KPI 的规范化计划（纯函数，无 IO）。"""
    kpiCode = str(kpi.kpi_code)
    if metric is None:
        raise NormalizationError(f"{kpiCode}: 未挂靠 ontology_metric，拒绝推断治理字段")
    # before 必须是**值快照**：直接存 ORM 上的 list/dict 会随 apply 一起被改写，
    # 报告里的 before/after 就变成同一个数（假取证）。
    before = {
        "numerator_columns": copy.deepcopy(list(kpi.numerator_columns or ())),
        "denominator_columns": copy.deepcopy(list(kpi.denominator_columns or ())),
        "grain_keys": copy.deepcopy(list(kpi.grain_keys or ())),
        "numerator_measure": copy.deepcopy(metric.numerator_measure),
        "denominator_measure": copy.deepcopy(metric.denominator_measure),
    }

    beforeResult = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))

    if (metric.execution_source or "").strip().upper() == "NONE":
        target = NormalizationPlan(
            kpi_code=kpiCode,
            metric_id=metric.id,
            tier=TIER_C,
            basis=BASIS_NONE_TIER_C,
            numerator_columns=(),
            denominator_columns=(),
            grain_keys=(),
            numerator_measure=None,
            denominator_measure=None,
            before=before,
            business_todo=(metric.formula or "").strip() or None,
            verdict_before=beforeResult.verdict,
        )
        afterResult = reconcileMetric(
            _governanceOf((), (), ()),
            techniqueOf(metric),
        )
        return _withAfter(target, afterResult, is_noop=_isNoop(kpi, target))

    allowed = frozenset(col.lower() for col in class_columns)
    numPlan, denPlan, writeNum, writeDen, basis = _deriveMeasures(kpiCode, metric)
    # 统一物理校验：已有 measure JSON 与派生表达式同一条线，任何一侧引用了
    # 源类里不存在的列都拒绝落库（不猜、不静默通过）。
    for side in (numPlan, denPlan):
        if side is not None and side.expr:
            _assertPhysical(side.expr, allowed, kpiCode)
    numeratorSpec = MeasureSpec.fromRaw(numPlan.toRaw()) if numPlan else None
    denominatorSpec = MeasureSpec.fromRaw(denPlan.toRaw()) if denPlan else None
    numeratorColumns = _measureColumns(numeratorSpec)
    denominatorColumns = _measureColumns(denominatorSpec)
    grainKeys = _tokens(metric.grain_keys)
    numeratorMeasure = numPlan.toRaw() if writeNum and numPlan is not None else None
    denominatorMeasure = denPlan.toRaw() if writeDen and denPlan is not None else None

    target = NormalizationPlan(
        kpi_code=kpiCode,
        metric_id=metric.id,
        tier=TIER_B,
        basis=basis,
        numerator_columns=numeratorColumns,
        denominator_columns=denominatorColumns,
        grain_keys=grainKeys,
        numerator_measure=numeratorMeasure,
        denominator_measure=denominatorMeasure,
        before=before,
        verdict_before=beforeResult.verdict,
    )
    afterResult = reconcileMetric(
        _governanceOf(numeratorColumns, denominatorColumns, grainKeys),
        _techniqueWithMeasures(metric, numeratorSpec, denominatorSpec),
    )
    return _withAfter(target, afterResult, is_noop=_isNoop(kpi, target))


def planNormalizations(
    kpis: Sequence[KpiCatalog],
    metrics_by_id: Mapping[int, OntologyMetric],
    class_columns_by_metric: Mapping[int, frozenset[str]],
) -> tuple[list[NormalizationPlan], list[str]]:
    """批量计划：保持输入顺序（报告可对照），单条失败即整体失败。

    返回 ``(plans, skipped_codes)``。``metric_id`` 为空的 KPI 是**合法的草稿**
    （``KpiCatalogCreate.metric_id`` 可空、``status`` 默认 DRAFT），跳过并返回，
    由调用方显式打印 —— 一条草稿不该让整个运维工具（含 dry-run）不可用。
    ``metric_id`` 非空但库里找不到对应 metric 属数据损坏，仍走
    :func:`deriveNormalization` 抛错（不静默跳过）。
    """
    plans: list[NormalizationPlan] = []
    skipped: list[str] = []
    for kpi in kpis:
        if kpi.metric_id is None:
            skipped.append(str(kpi.kpi_code))
            continue
        plans.append(
            deriveNormalization(
                kpi,
                metrics_by_id.get(kpi.metric_id),
                class_columns_by_metric.get(kpi.metric_id, frozenset()),
            )
        )
    return plans, skipped


__all__ = [
    "BASIS_FORMULA_MEASURE",
    "BASIS_FORMULA_RATIO",
    "BASIS_MEASURE_JSON",
    "BASIS_NONE_TIER_C",
    "MeasurePlan",
    "NormalizationError",
    "NormalizationPlan",
    "deriveNormalization",
    "planNormalizations",
    "splitTopLevelDivision",
    "stripAliases",
]
