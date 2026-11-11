"""KPI Tier A 执行路由与确定性执行（设计 §6）。

路由只读 ontology_metric.execution_source，不嗅探 formula 文本；开关关闭时
整个模块不参与决策（调用方回旧 L1 行为）。
"""

from __future__ import annotations

from dataclasses import dataclass

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
TIER_LEGACY = "LEGACY"

SOURCE_ESCAPE_HATCH = "ESCAPE_HATCH"
SOURCE_COMPILED = "COMPILED"
SOURCE_NONE = "NONE"

REASON_TIER_A_DISABLED = "tier_a_disabled"
REASON_KPI_NOT_FOUND = "kpi_not_found"
REASON_METRIC_MISSING = "metric_missing"
REASON_EXECUTION_SOURCE_MISSING = "execution_source_missing"
REASON_EXECUTION_SOURCE_NONE = "execution_source_none"
REASON_EXECUTION_SOURCE_UNKNOWN = "execution_source_unknown"
REASON_COMPILED_NOT_SUPPORTED = "compiled_not_supported"
REASON_DATASOURCE_MISSING = "datasource_missing"
REASON_EXPLICIT_TIME_SCOPE = "explicit_time_scope"
REASON_RECONCILE_NOT_ELIGIBLE = "reconcile_not_eligible"
REASON_FORMULA_EMPTY = "formula_empty"


@dataclass(frozen=True)
class KpiExecutionPlan:
    tier: str
    reason: str | None = None


def planKpiExecution(
    *,
    switch_on: bool,
    kpi_found: bool,
    metric_present: bool,
    execution_source: str | None,
    tier_a_eligible: bool,
    has_datasource: bool,
    formula_present: bool,
    has_explicit_time_scope: bool,
) -> KpiExecutionPlan:
    """按 §6.1/§6.2/§6.4 顺序短路（见 plan.md §3 决策表）。"""
    if not switch_on:
        return KpiExecutionPlan(TIER_LEGACY, REASON_TIER_A_DISABLED)
    if not kpi_found:
        return KpiExecutionPlan(TIER_LEGACY, REASON_KPI_NOT_FOUND)
    if not metric_present:
        return KpiExecutionPlan(TIER_C, REASON_METRIC_MISSING)
    source = (execution_source or "").strip().upper()
    if not source:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_MISSING)
    if source == SOURCE_NONE:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_NONE)
    if source == SOURCE_COMPILED:
        return KpiExecutionPlan(TIER_B, REASON_COMPILED_NOT_SUPPORTED)
    if source != SOURCE_ESCAPE_HATCH:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_UNKNOWN)
    if not has_datasource:
        return KpiExecutionPlan(TIER_C, REASON_DATASOURCE_MISSING)
    if has_explicit_time_scope:
        return KpiExecutionPlan(TIER_B, REASON_EXPLICIT_TIME_SCOPE)
    if not formula_present:
        return KpiExecutionPlan(TIER_B, REASON_FORMULA_EMPTY)
    if not tier_a_eligible:
        return KpiExecutionPlan(TIER_B, REASON_RECONCILE_NOT_ELIGIBLE)
    return KpiExecutionPlan(TIER_A)
