"""运行时 ``planKpiExecution`` 与诊断 ``_tierOf`` 的 Tier 判定 parity。

计划 §2 不变量 ③ 要求两侧同源（同一份 ``reconcileMetric`` 输入）。这里用**内存对象**
（不连库）构造 KpiCatalog / OntologyMetric，经 ``governanceSpecOf`` + ``techniqueOf``
+ ``reconcileMetric`` 得到 ReconcileResult，再分别走运行时与诊断两条判定。
"""

from __future__ import annotations

import pytest

from app.api.v1 import kpi_diagnose
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_execution_service import (
    SOURCE_COMPILED,
    SOURCE_ESCAPE_HATCH,
    SOURCE_NONE,
    planKpiExecution,
)
from app.services.kpi_metric_reconcile import reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf


def _kpi(*, datasource_id: int | None = 1) -> KpiCatalog:
    return KpiCatalog(
        kpi_code="KPI_PARITY",
        kpi_name="一致性测试指标",
        business_definition="parity",
        datasource_id=datasource_id,
        numerator_columns=["cnt"],
        denominator_columns=[],
        grain_keys=["grain_key"],
    )


def _eligible_metric(
    source: str, *, formula: str = "SELECT COUNT(cnt) AS cnt FROM t"
) -> OntologyMetric:
    """层 1/2/3 全 pass：结构化列 ``cnt`` 出现在 formula 中，治理列 == 技术列。

    ``formula`` 可传空串构造生产 COMPILED 形态（编译器的可执行性不依赖 formula 文本）。
    """
    return OntologyMetric(
        metric_name="parity-eligible",
        formula=formula,
        metric_type="MEASURE",
        numerator_measure={"agg": "COUNT", "expr": "cnt"},
        grain_keys=["grain_key"],
        execution_source=source,
    )


def _ineligible_metric(source: str) -> OntologyMetric:
    """层 1/3 degraded（``cnt`` 不在 formula 中）⇒ verdict pass 但不可 Tier A。"""
    return OntologyMetric(
        metric_name="parity-ineligible",
        formula="SELECT COUNT(*) AS other FROM t",
        metric_type="MEASURE",
        numerator_measure={"agg": "COUNT", "expr": "cnt"},
        grain_keys=["grain_key"],
        execution_source=source,
    )


def _result_of(metric: OntologyMetric, kpi: KpiCatalog):
    return reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))


def _runtime_plan(metric: OntologyMetric, *, has_datasource: bool = True):
    result = _result_of(metric, _kpi(datasource_id=1 if has_datasource else None))
    source = (metric.execution_source or "").strip().upper()
    # 与 chat_stream_output 同款覆盖：COMPILED 的可执行性来自编译器，空 formula 正常。
    formulaPresent = bool((metric.formula or "").strip()) or source == SOURCE_COMPILED
    return planKpiExecution(
        switch_on=True,
        kpi_found=True,
        metric_present=True,
        execution_source=metric.execution_source,
        tier_a_eligible=result.isTierAEligible,
        has_datasource=has_datasource,
        formula_present=formulaPresent,
        has_explicit_time_scope=False,
    )


@pytest.mark.parametrize("source", [SOURCE_ESCAPE_HATCH, SOURCE_COMPILED, SOURCE_NONE])
@pytest.mark.parametrize("eligible", [True, False])
def test_runtime_and_diagnose_tier_agree(source: str, eligible: bool) -> None:
    """ESCAPE_HATCH / COMPILED / NONE × eligible 两态：两侧 Tier 必须一致。"""
    metric = _eligible_metric(source) if eligible else _ineligible_metric(source)
    kpi = _kpi()
    result = _result_of(metric, kpi)
    assert result.isTierAEligible is eligible

    plan = _runtime_plan(metric)
    diagnoseTier = kpi_diagnose._tierOf(result, metric)
    assert plan.tier == diagnoseTier, (
        f"source={source!r} eligible={eligible}: "
        f"runtime={plan.tier}/{plan.reason} diagnose={diagnoseTier}"
    )


def test_compiled_eligible_routes_to_tier_a() -> None:
    """COMPILED 是受支持的执行源：对账可直通 ⇒ 运行时与诊断都判 A。"""
    metric = _eligible_metric(SOURCE_COMPILED)
    assert _result_of(metric, _kpi()).isTierAEligible is True

    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("A", None)
    assert kpi_diagnose._tierOf(_result_of(metric, _kpi()), metric) == "A"


def test_compiled_ineligible_stays_tier_b() -> None:
    """COMPILED 与 ESCAPE_HATCH 同分支：对账不合格仍降级 B。"""
    metric = _ineligible_metric(SOURCE_COMPILED)
    assert _result_of(metric, _kpi()).isTierAEligible is False

    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("B", "reconcile_not_eligible")
    assert kpi_diagnose._tierOf(_result_of(metric, _kpi()), metric) == "B"


def test_empty_source_is_tier_c_on_both_sides() -> None:
    """空 source：运行时 / 诊断一致判 C（本任务收口的历史分歧）。"""
    metric = _eligible_metric("")
    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("C", "execution_source_missing")
    assert kpi_diagnose._tierOf(_result_of(metric, _kpi()), metric) == "C"


def test_none_source_is_tier_c_on_both_sides() -> None:
    metric = _eligible_metric(SOURCE_NONE)
    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("C", "execution_source_none")
    assert kpi_diagnose._tierOf(_result_of(metric, _kpi()), metric) == "C"


@pytest.mark.parametrize("eligible", [True, False])
def test_unknown_source_is_tier_c_on_both_sides(eligible: bool) -> None:
    metric = _eligible_metric("WEIRD") if eligible else _ineligible_metric("WEIRD")
    assert _result_of(metric, _kpi()).isTierAEligible is eligible

    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("C", "execution_source_unknown")
    assert kpi_diagnose._tierOf(_result_of(metric, _kpi()), metric) == "C"


@pytest.mark.parametrize("source", [SOURCE_ESCAPE_HATCH, SOURCE_COMPILED])
@pytest.mark.parametrize("formula", ["", "SELECT COUNT(cnt) AS cnt FROM t"])
def test_formula_presence_parity(source: str, formula: str) -> None:
    """formula 空/非空 × 两种可执行源：运行时与诊断 Tier 必须一致。"""
    metric = _eligible_metric(source, formula=formula)
    result = _result_of(metric, _kpi())
    assert result.isTierAEligible is True

    plan = _runtime_plan(metric)
    diagnoseTier = kpi_diagnose._tierOf(result, metric)
    assert plan.tier == diagnoseTier, (
        f"source={source!r} formula={formula!r}: "
        f"runtime={plan.tier}/{plan.reason} diagnose={diagnoseTier}"
    )


def test_escape_hatch_empty_formula_is_tier_b_on_both_sides() -> None:
    """T4.1：ESCAPE_HATCH 空 formula ⇒ 运行时 B/formula_empty，诊断 B。"""
    metric = _eligible_metric(SOURCE_ESCAPE_HATCH, formula="   ")
    result = _result_of(metric, _kpi())
    assert result.isTierAEligible is True

    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("B", "formula_empty")
    assert kpi_diagnose._tierOf(result, metric) == "B"


def test_compiled_empty_formula_is_tier_a_on_both_sides() -> None:
    """T4.2：生产 COMPILED 形态（空 formula + 对账合格）⇒ 运行时与诊断都判 A。"""
    metric = _eligible_metric(SOURCE_COMPILED, formula="")
    result = _result_of(metric, _kpi())
    assert result.isTierAEligible is True

    plan = _runtime_plan(metric)
    assert (plan.tier, plan.reason) == ("A", None)
    assert kpi_diagnose._tierOf(result, metric) == "A"


def test_known_divergence_datasource_missing() -> None:
    """显式钉住仍**已知且本批不改**的分歧（计划 §3 原文）。

    ``has_datasource=False``：运行时 C / 诊断 A（诊断没有 datasource 视图）。
    P5 治理收口跟进；若有人改了任一侧行为，这条会红，请同步更新 summary.md。
    """
    metricEscape = _eligible_metric(SOURCE_ESCAPE_HATCH)
    kpiWithoutDs = _kpi(datasource_id=None)
    resultEscape = _result_of(metricEscape, kpiWithoutDs)
    planNoDs = _runtime_plan(metricEscape, has_datasource=False)
    assert planNoDs.tier == "C"
    assert planNoDs.reason == "datasource_missing"
    assert kpi_diagnose._tierOf(resultEscape, metricEscape) == "A"
