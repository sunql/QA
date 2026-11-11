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

import re
from collections.abc import Iterable, Mapping, Sequence
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
from app.services.nl2sql_semantic_guard import extractSqlIdentifiers

_ALIAS_PREFIX = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.")
_TRUNC_CALL = re.compile(r"\bTRUNC\s*\(\s*([^()]*?)\s*\)", re.IGNORECASE)
_OUTER_AGG = re.compile(
    r"^\s*SELECT\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*FROM\b",
    re.IGNORECASE | re.DOTALL,
)

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
    """去掉 ``R.COL`` / ``THBI.TABLE`` 这类前缀。"""
    return _ALIAS_PREFIX.sub("", expr)


def unwrapTrunc(expr: str) -> str:
    """``TRUNC(X)`` → ``(X)``：列集合不变，但让派生表达式可读。"""
    return _TRUNC_CALL.sub(r"(\1)", expr)


def splitTopLevelDivision(expr: str) -> tuple[str, str] | None:
    """只在括号深度 0 且**恰好一个** ``/`` 时拆分；否则返回 None。"""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(expr):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "/" and depth == 0:
            parts.append(expr[start:index])
            start = index + 1
    if len(parts) != 1:
        return None
    parts.append(expr[start:])
    return parts[0].strip(), parts[1].strip()


def _normalizeExpr(expr: str) -> str:
    return unwrapTrunc(stripAliases(expr)).strip()


def _assertPhysical(
    expression: str, allowedColumns: frozenset[str], kpiCode: str
) -> None:
    """派生表达式的列必须能在源类物理列里找到，否则拒绝（不猜）。"""
    tokens = extractSqlIdentifiers(expression)
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


def _deriveMeasures(
    kpiCode: str,
    metric: OntologyMetric,
    allowedColumns: frozenset[str],
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

    outer = _OUTER_AGG.match(metric.formula or "")
    if outer is None:
        raise NormalizationError(
            f"{kpiCode}: metric {metric.id} 缺 measure 且 formula 无法解析外层聚合"
        )
    agg = outer.group(1).upper()
    body = _normalizeExpr(outer.group(2))

    if metric.metric_type == "MEASURE":
        _assertPhysical(body, allowedColumns, kpiCode)
        return (
            MeasurePlan(agg=agg, expr=body),
            _existingPlan(denominator),
            True,
            False,
            BASIS_FORMULA_MEASURE,
        )

    if metric.metric_type == "RATIO" and (numerator is None or denominator is None):
        split = splitTopLevelDivision(body)
        if split is None:
            raise NormalizationError(
                f"{kpiCode}: RATIO 缺 measure，且 formula 参数体没有唯一的顶层 '/'"
            )
        left, right = split
        _assertPhysical(left, allowedColumns, kpiCode)
        _assertPhysical(right, allowedColumns, kpiCode)
        return (
            MeasurePlan(agg=agg, expr=left),
            MeasurePlan(agg=agg, expr=right),
            True,
            True,
            BASIS_FORMULA_RATIO,
        )

    raise NormalizationError(
        f"{kpiCode}: metric {metric.id}（{metric.metric_type}）的 measure 组合"
        "无法用现有规则派生"
    )


def _tierOf(result: Any) -> str:
    """Tier A = 层 2 pass 且层 1/3 无降级；其余为 C。"""
    if result.isTierAEligible:
        return TIER_A
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
    """目标值 == 现值且无需补 measure ⇒ 重跑不产生任何写入。"""
    return (
        _tokens(kpi.numerator_columns) == plan.numerator_columns
        and _tokens(kpi.denominator_columns) == plan.denominator_columns
        and _tokens(kpi.grain_keys) == plan.grain_keys
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
    before = {
        "numerator_columns": kpi.numerator_columns,
        "denominator_columns": kpi.denominator_columns,
        "grain_keys": kpi.grain_keys,
        "numerator_measure": metric.numerator_measure if metric else None,
        "denominator_measure": metric.denominator_measure if metric else None,
    }
    if metric is None:
        raise NormalizationError(f"{kpiCode}: 未挂靠 ontology_metric，拒绝推断治理字段")

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
    numPlan, denPlan, writeNum, writeDen, basis = _deriveMeasures(
        kpiCode, metric, allowed
    )
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
) -> list[NormalizationPlan]:
    """批量计划：保持输入顺序（报告可对照），单条失败即整体失败。"""
    return [
        deriveNormalization(
            kpi,
            metrics_by_id.get(kpi.metric_id) if kpi.metric_id is not None else None,
            class_columns_by_metric.get(kpi.metric_id, frozenset()),
        )
        for kpi in kpis
    ]


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
    "unwrapTrunc",
]
