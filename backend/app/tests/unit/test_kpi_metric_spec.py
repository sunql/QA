"""`kpi_metric_spec` —— ORM 行 → 三层对账输入的**唯一切换点**（R6）。

为什么要有**直接**单测：契约注入、发布闸门、真机验证脚本三处都走这里；
若只做间接覆盖，任何一处的语义变化都可能被另一处的绿测试掩盖。
"""

from __future__ import annotations

from app.domain.enums import MetricExecutionSource, MetricType
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_metric_reconcile import MetricTechnique
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf


def _kpi(**overrides: object) -> KpiCatalog:
    values: dict[str, object] = {
        "kpi_code": "KPI_X",
        "kpi_name": "测试 KPI",
        "numerator_columns": ["col_a"],
        "denominator_columns": ["col_b"],
        "grain_keys": ["supplier", "month"],
    }
    values.update(overrides)
    return KpiCatalog(**values)


def _metric(**overrides: object) -> OntologyMetric:
    values: dict[str, object] = {
        "metric_name": "m",
        "formula": "SELECT 1",
        "metric_type": MetricType.RATIO.value,
        "numerator_measure": {"agg": "count", "expr": "RECEIPT_DATE"},
        "denominator_measure": {"agg": "count", "expr": "PROMISED_RECEIPT_DATE"},
        "aggregation_order": "WEIGHTED_RATIO",
        "grain_keys": ["SUPPLIER_CODE"],
        "filter_expression": "X > 1",
        "execution_source": MetricExecutionSource.ESCAPE_HATCH.value,
    }
    values.update(overrides)
    return OntologyMetric(**values)


# ── governanceSpecOf ────────────────────────────────────────────────


def test_governance_spec_maps_columns_to_tuples() -> None:
    spec = governanceSpecOf(_kpi())
    assert spec.numerator_columns == ("col_a",)
    assert spec.denominator_columns == ("col_b",)
    assert spec.grain_keys == ("supplier", "month")
    assert isinstance(spec.numerator_columns, tuple)


def test_governance_spec_handles_null_columns() -> None:
    spec = governanceSpecOf(
        _kpi(numerator_columns=None, denominator_columns=None, grain_keys=None)
    )
    assert spec.numerator_columns == ()
    assert spec.denominator_columns == ()
    assert spec.grain_keys == ()


# ── techniqueOf ─────────────────────────────────────────────────────


def test_technique_of_none_returns_empty_technique() -> None:
    """空值契约：None ⇒ 空技术形态（对账判 unknown，不是异常）。"""
    technique = techniqueOf(None)
    assert isinstance(technique, MetricTechnique)
    assert technique.metric_type == MetricType.MEASURE.value
    assert technique.numerator is None
    assert technique.denominator is None
    assert technique.grain_keys == ()
    assert technique.formula is None


def test_technique_of_metric_maps_every_field() -> None:
    technique = techniqueOf(_metric())
    assert technique.metric_type == MetricType.RATIO.value
    assert technique.numerator is not None
    assert technique.numerator.agg == "COUNT"
    assert technique.numerator.expr == "RECEIPT_DATE"
    assert technique.denominator is not None
    assert technique.denominator.expr == "PROMISED_RECEIPT_DATE"
    assert technique.aggregation_order == "WEIGHTED_RATIO"
    assert technique.grain_keys == ("SUPPLIER_CODE",)
    assert technique.filter_expression == "X > 1"
    assert technique.formula == "SELECT 1"
    assert technique.execution_source == MetricExecutionSource.ESCAPE_HATCH.value


def test_technique_of_metric_defaults_blank_metric_type_to_measure() -> None:
    technique = techniqueOf(_metric(metric_type=None))
    assert technique.metric_type == MetricType.MEASURE.value


def test_technique_of_metric_treats_malformed_measure_as_none() -> None:
    """非 dict / 缺 agg 的度量 JSON ⇒ 该侧为 None（按不可判处理）。"""
    technique = techniqueOf(
        _metric(numerator_measure="not-a-dict", denominator_measure={"expr": "X"})
    )
    assert technique.numerator is None
    assert technique.denominator is None


def test_technique_of_metric_keeps_null_grain_as_empty_tuple() -> None:
    technique = techniqueOf(_metric(grain_keys=None))
    assert technique.grain_keys == ()
