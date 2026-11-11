"""KPI ``time_grain`` / ``time_column`` 回填的**纯函数**部分（零 IO）。

派生规则（P3 设计 §7，裁决 R3/R6）：

- ``time_grain``：``deriveTimeGrain(kpi.grain)`` 自由文本确定性推导（R6）；
  ``grain`` 无时间词 ⇒ 不写。已有显式值且与推导不同 ⇒ 保留人工值、报告冲突
  （显式列优先，见 ``kpi_metric_spec.governanceSpecOf``）。
- ``time_column``（R3，**不猜**）：``grain_keys`` 中经 resolver
  ``timeCandidates`` 过滤后的日期列 → 唯一 ⇒ 写；否则 measure expr / formula
  实际引用的日期列 → 唯一 ⇒ 写；多候选/零候选 ⇒ 不写，报告候选清单。

IO / CLI 在 ``scripts/kpi_backfill_time_grain.py``（骨架见
``scripts/kpi_normalize_governance.py``）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.services.formula_parser import parseFormula
from app.services.kpi_alignment_resolver import (
    SourceProperty,
    deriveTimeGrain,
    resolveGovernanceTokens,
)
from app.services.nl2sql_semantic_guard import measureColumnIdentifiers

# 与 kpi_alignment_resolver._DATE_DATA_TYPES 同值：本模块不 import 私有常量。
_DATE_DATA_TYPES: frozenset[str] = frozenset({"DATE", "DATETIME", "TIMESTAMP"})
_DATE_COLUMN_MARKER = "DATE"
_TS_COLUMN_SUFFIX = "_TS"


@dataclass(frozen=True)
class SchemaAvailability:
    """0143 三列在当前库中是否存在。"""

    kpiTimeGrain: bool
    metricTimeGrain: bool
    metricTimeColumn: bool

    @property
    def isComplete(self) -> bool:
        return self.kpiTimeGrain and self.metricTimeGrain and self.metricTimeColumn

    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.kpiTimeGrain:
            missing.append("kpi_catalog.time_grain")
        if not self.metricTimeGrain:
            missing.append("ontology_metric.time_grain")
        if not self.metricTimeColumn:
            missing.append("ontology_metric.time_column")
        return tuple(missing)


@dataclass(frozen=True)
class KpiSnapshot:
    """回填只关心 KPI 的粒度文本 + 粒度键 + 挂靠 metric。"""

    kpi_id: int
    kpi_code: str
    grain: str | None
    grain_keys: tuple[str, ...]
    metric_id: int | None
    time_grain: str | None


@dataclass(frozen=True)
class MetricSnapshot:
    metric_id: int
    formula: str | None
    numerator_measure: object
    denominator_measure: object
    time_grain: str | None
    time_column: str | None


@dataclass(frozen=True)
class TimeColumnDerivation:
    """R3 候选推导结果：``column=None`` ⇒ 不写（多候选/零候选，不猜）。"""

    column: str | None
    basis: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackfillPlan:
    kpi_id: int
    kpi_code: str
    metric_id: int | None
    grain_text: str | None
    before_kpi_time_grain: str | None
    before_metric_time_grain: str | None
    before_metric_time_column: str | None
    target_kpi_time_grain: str | None
    target_metric_time_grain: str | None
    target_metric_time_column: str | None
    time_column_basis: str
    time_column_candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_kpi_grain_change(self) -> bool:
        return _grainDiffers(self.before_kpi_time_grain, self.target_kpi_time_grain)

    @property
    def has_metric_grain_change(self) -> bool:
        return _grainDiffers(self.before_metric_time_grain, self.target_metric_time_grain)

    @property
    def has_metric_column_change(self) -> bool:
        return _columnDiffers(
            self.before_metric_time_column, self.target_metric_time_column
        )

    @property
    def has_changes(self) -> bool:
        return (
            self.has_kpi_grain_change
            or self.has_metric_grain_change
            or self.has_metric_column_change
        )


def _grainDiffers(before: str | None, target: str | None) -> bool:
    """target=None 表示「不写」；比较大小写不敏感（值域统一小写）。"""
    return target is not None and (before or "").strip().lower() != target


def _columnDiffers(before: str | None, target: str | None) -> bool:
    """target=None 表示「不写」；列名比较大小写不敏感。"""
    return target is not None and (before or "").strip().upper() != target.strip().upper()


# ── time_column（R3） ────────────────────────────────────────────────


def _dataTypesByColumn(properties: Sequence[SourceProperty]) -> dict[str, str]:
    """``source_column`` / ``property_name``（大写）→ data_type（大写）。"""
    result: dict[str, str] = {}
    for prop in properties:
        dataType = (prop.data_type or "").strip().upper()
        if prop.source_column:
            result[prop.source_column.strip().upper()] = dataType
        if prop.property_name:
            result.setdefault(prop.property_name.strip().upper(), dataType)
    return result


def _columnCasing(properties: Sequence[SourceProperty]) -> dict[str, str]:
    """``source_column`` 大写 → 库里原始大小写（写回用原始形态）。"""
    return {
        prop.source_column.strip().upper(): prop.source_column.strip()
        for prop in properties
        if prop.source_column
    }


def _isDateIdentifier(
    identifier: str,
    dateCandidates: Mapping[str, str],
    dataTypes: Mapping[str, str],
    knownColumns: Mapping[str, str],
) -> bool:
    """与 resolver ``_isDateColumn`` 同判定，但**只信已知物理列**。

    measure / formula 的标识符提取是「整段 SQL 的裸词」：表名、CTE 名、别名、
    关键字都会进来（``FROM DWD_DATE_DIM`` / ``WITH recent_date AS``）。名称启发式
    （含 DATE / 以 ``_TS`` 结尾）只允许作用于 ``ontology_property.source_column``
    已知的物理列；否则 ``DWD_DATE_DIM`` 会被当成 ``time_column`` 写入，或与真实
    日期列一起把候选判成 ambiguous，错误阻断合法回填。
    """
    upper = identifier.strip().upper()
    if not upper:
        return False
    if upper in dateCandidates:
        return True
    if upper not in knownColumns:
        return False
    if dataTypes.get(upper) in _DATE_DATA_TYPES:
        return True
    return _DATE_COLUMN_MARKER in upper or upper.endswith(_TS_COLUMN_SUFFIX)


def _referencedColumns(metric: MetricSnapshot) -> tuple[str, ...]:
    """measure expr ∪ formula 实际引用的标识符（去重保序，大写键）。"""
    refs: list[str] = []
    for raw in (metric.numerator_measure, metric.denominator_measure):
        expr = raw.get("expr") if isinstance(raw, dict) else None
        if isinstance(expr, str):
            refs.extend(measureColumnIdentifiers(expr))
    refs.extend(column for _alias, column in parseFormula(metric.formula).column_refs)
    deduped: dict[str, str] = {}
    for ref in refs:
        cleaned = ref.strip()
        if cleaned:
            deduped.setdefault(cleaned.upper(), cleaned)
    return tuple(deduped.values())


def deriveTimeColumn(
    grainKeys: Sequence[str] | None,
    metric: MetricSnapshot | None,
    properties: Sequence[SourceProperty],
) -> TimeColumnDerivation:
    """R3：grain_keys 日期 token 优先 → measure/formula 引用列 → 唯一才写。

    多候选/零候选一律 ``column=None``（不猜），并把候选清单交回报告。
    """
    outcome = resolveGovernanceTokens(tuple(grainKeys or ()), tuple(properties))
    dateCandidates = {column.upper(): column for column in outcome.timeCandidates}
    casing = _columnCasing(properties)
    dataTypes = _dataTypesByColumn(properties)

    grainCandidates = tuple(
        sorted(
            {
                dateCandidates[column.upper()]
                for _token, column in outcome.resolved
                if column.upper() in dateCandidates
            }
        )
    )
    if len(grainCandidates) == 1:
        return TimeColumnDerivation(
            column=grainCandidates[0],
            basis="grain_keys",
            candidates=grainCandidates,
        )

    if metric is None:
        return TimeColumnDerivation(
            column=None, basis="no-metric", candidates=grainCandidates
        )

    measureCandidates: set[str] = set()
    for ref in _referencedColumns(metric):
        if not _isDateIdentifier(ref, dateCandidates, dataTypes, casing):
            continue
        upper = ref.upper()
        measureCandidates.add(dateCandidates.get(upper) or casing.get(upper) or upper)
    measureTuple = tuple(sorted(measureCandidates))
    if len(measureTuple) == 1:
        return TimeColumnDerivation(
            column=measureTuple[0], basis="measure_formula", candidates=measureTuple
        )

    allCandidates = tuple(sorted(set(grainCandidates) | set(measureTuple)))
    return TimeColumnDerivation(
        column=None,
        basis="ambiguous" if allCandidates else "no-candidates",
        candidates=allCandidates,
    )


# ── 计划 ─────────────────────────────────────────────────────────────


def _conflictNote(fieldName: str, current: str | None, derived: str) -> str:
    return (
        f"{fieldName} 已有显式值 {current!r}，与 grain 推导 {derived!r} 不同："
        "保留人工值（显式列优先）"
    )


def planBackfills(
    kpis: Sequence[KpiSnapshot],
    metrics: Mapping[int, MetricSnapshot],
    propertiesByMetric: Mapping[int, tuple[SourceProperty, ...]],
) -> list[BackfillPlan]:
    """逐 KPI 生成回填计划（纯函数：同输入同输出，不做 IO）。"""
    plans: list[BackfillPlan] = []
    for kpi in kpis:
        metric = metrics.get(kpi.metric_id) if kpi.metric_id is not None else None
        properties = (
            propertiesByMetric.get(kpi.metric_id, ()) if kpi.metric_id is not None else ()
        )
        notes: list[str] = []

        derivedGrain = deriveTimeGrain(kpi.grain)
        targetKpiGrain: str | None = None
        targetMetricGrain: str | None = None
        if derivedGrain is None:
            if kpi.time_grain is not None:
                notes.append("grain 无时间词：保留已有 kpi_catalog.time_grain")
            if metric is not None and metric.time_grain is not None:
                notes.append("grain 无时间词：保留已有 ontology_metric.time_grain")
        else:
            derivedValue = derivedGrain.value
            if kpi.time_grain is None:
                targetKpiGrain = derivedValue
            elif kpi.time_grain.strip().lower() != derivedValue:
                notes.append(
                    _conflictNote("kpi_catalog.time_grain", kpi.time_grain, derivedValue)
                )
            if metric is None:
                if kpi.metric_id is None:
                    notes.append("未挂靠 ontology_metric：只回填 kpi_catalog.time_grain")
                else:
                    notes.append(
                        f"挂靠 metric_id={kpi.metric_id} 在库中不存在："
                        "只回填 kpi_catalog.time_grain"
                    )
            elif metric.time_grain is None:
                targetMetricGrain = derivedValue
            elif metric.time_grain.strip().lower() != derivedValue:
                notes.append(
                    _conflictNote(
                        "ontology_metric.time_grain", metric.time_grain, derivedValue
                    )
                )

        derivation = deriveTimeColumn(kpi.grain_keys, metric, properties)
        targetColumn: str | None = None
        if metric is None:
            if derivation.candidates:
                notes.append(
                    f"time_column 候选 {list(derivation.candidates)!r}："
                    "无 ontology_metric 行，未写"
                )
        elif metric.time_column is not None:
            if derivation.column is not None and not _columnDiffers(
                metric.time_column, derivation.column
            ):
                pass  # 已是同值（幂等，无需 note）
            elif derivation.column is not None:
                notes.append(
                    f"ontology_metric.time_column 已有显式值 {metric.time_column!r}，"
                    f"与推导 {derivation.column!r} 不同：保留人工值"
                )
        else:
            targetColumn = derivation.column
            if targetColumn is None:
                notes.append(
                    f"time_column 不写（{derivation.basis}）："
                    f"候选 {list(derivation.candidates)!r}（多候选/零候选不猜，"
                    "由治理侧人工指定）"
                )

        plans.append(
            BackfillPlan(
                kpi_id=kpi.kpi_id,
                kpi_code=kpi.kpi_code,
                metric_id=kpi.metric_id,
                grain_text=kpi.grain,
                before_kpi_time_grain=kpi.time_grain,
                before_metric_time_grain=None if metric is None else metric.time_grain,
                before_metric_time_column=None if metric is None else metric.time_column,
                target_kpi_time_grain=targetKpiGrain,
                target_metric_time_grain=targetMetricGrain,
                target_metric_time_column=targetColumn,
                time_column_basis=derivation.basis,
                time_column_candidates=derivation.candidates,
                notes=tuple(notes),
            )
        )
    return plans


# ── 报告 ─────────────────────────────────────────────────────────────


def _renderValue(value: str | None, available: bool) -> str:
    if not available:
        return "n/a(0143)"
    return "null" if value is None else repr(value)


def _renderTarget(value: str | None) -> str:
    return "<不写>" if value is None else str(value)


def renderReport(
    plans: Sequence[BackfillPlan], availability: SchemaAvailability
) -> str:
    """stdout 报告：逐 KPI before/after + 推导依据 + time_column 候选清单。"""
    lines: list[str] = []
    countChanges = 0
    for index, plan in enumerate(plans, start=1):
        if plan.has_changes:
            countChanges += 1
        metricPart = "None" if plan.metric_id is None else str(plan.metric_id)
        lines.append(f"[{index}/{len(plans)}] {plan.kpi_code} (metric={metricPart})")
        lines.append(
            f"    grain {plan.grain_text!r} -> time_grain="
            f"{_renderTarget(plan.target_kpi_time_grain)} "
            f"basis=deriveTimeGrain({plan.grain_text!r})"
        )
        lines.append(
            f"    kpi.time_grain: "
            f"{_renderValue(plan.before_kpi_time_grain, availability.kpiTimeGrain)} "
            f"-> {_renderTarget(plan.target_kpi_time_grain)}"
        )
        lines.append(
            f"    metric.time_grain: "
            f"{_renderValue(plan.before_metric_time_grain, availability.metricTimeGrain)} "
            f"-> {_renderTarget(plan.target_metric_time_grain)}"
        )
        lines.append(
            f"    metric.time_column: "
            f"{_renderValue(plan.before_metric_time_column, availability.metricTimeColumn)} "
            f"-> {_renderTarget(plan.target_metric_time_column)} "
            f"basis={plan.time_column_basis} "
            f"candidates={list(plan.time_column_candidates)!r}"
        )
        for note in plan.notes:
            lines.append(f"    note: {note}")
    lines.append(
        f"summary: changes={countChanges} noop={len(plans) - countChanges} "
        f"total={len(plans)}"
    )
    return "\n".join(lines)


__all__ = [
    "BackfillPlan",
    "KpiSnapshot",
    "MetricSnapshot",
    "SchemaAvailability",
    "TimeColumnDerivation",
    "deriveTimeColumn",
    "planBackfills",
    "renderReport",
]
