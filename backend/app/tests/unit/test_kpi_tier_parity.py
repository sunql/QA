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


def _eligible_metric(source: str) -> OntologyMetric:
    """层 1/2/3 全 pass：结构化列 ``cnt`` 出现在 formula 中，治理列 == 技术列。"""
    return OntologyMetric(
        metric_name="parity-eligible",
        formula="SELECT COUNT(cnt) AS cnt FROM t",
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
    return planKpiExecution(
        switch_on=True,
        kpi_found=True,
        metric_present=True,
        execution_source=metric.execution_source,
        tier_a_eligible=result.isTierAEligible,
        has_datasource=has_datasource,
        formula_present=bool((metric.formula or "").strip()),
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


def test_known_divergences_are_documented() -> None:
    """显式钉住两处**已知且本批不改**的分歧（计划 §3 原文）。

    ① ``has_datasource=False``：运行时 C / 诊断 A（诊断没有 datasource 视图）；
    ② ``execution_source`` 为空或未知值：运行时 C / 诊断 B（诊断侧来源不是
       ESCAPE_HATCH ⇒ 不可 Tier A）。eligible True/False 两态都钉住。

    P5 治理收口跟进；若有人改了任一侧行为，这条会红，请同步更新 summary.md。
    """
    # ① 无数据源绑定
    metricEscape = _eligible_metric(SOURCE_ESCAPE_HATCH)
    kpiWithoutDs = _kpi(datasource_id=None)
    resultEscape = _result_of(metricEscape, kpiWithoutDs)
    planNoDs = _runtime_plan(metricEscape, has_datasource=False)
    assert planNoDs.tier == "C"
    assert planNoDs.reason == "datasource_missing"
    assert kpi_diagnose._tierOf(resultEscape, metricEscape) == "A"

    # ② 来源为空 / 未知：eligible True 与 False 都必须落在运行时 C / 诊断 B
    for source in ("", "WEIRD"):
        for eligible in (True, False):
            metric = _eligible_metric(source) if eligible else _ineligible_metric(source)
            result = _result_of(metric, _kpi())
            assert result.isTierAEligible is eligible
            plan = _runtime_plan(metric)
            assert plan.tier == "C"
            assert kpi_diagnose._tierOf(result, metric) == "B"
