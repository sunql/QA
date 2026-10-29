"""KPI 指标技术形态 —— DTO 形状与 camelCase alias 断言。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.schemas import (
    KpiCatalogCreate,
    KpiCatalogRead,
    KpiCatalogUpdate,
    MeasureSpecSchema,
    OntologyMetricCreate,
    OntologyMetricRead,
    OntologyMetricUpdate,
)


def test_ontology_metric_create_has_new_fields() -> None:
    names = set(OntologyMetricCreate.model_fields)
    for f in (
        "metric_type",
        "numerator_measure",
        "denominator_measure",
        "aggregation_order",
        "grain_keys",
        "filter_expression",
        "status",
        "owner",
        "execution_source",
    ):
        assert f in names, f"OntologyMetricCreate 缺字段 {f}"


def test_ontology_metric_update_all_fields_optional() -> None:
    names = set(OntologyMetricUpdate.model_fields)
    for f in (
        "metric_type",
        "numerator_measure",
        "denominator_measure",
        "aggregation_order",
        "grain_keys",
        "filter_expression",
        "status",
        "owner",
        "execution_source",
    ):
        assert f in names, f"OntologyMetricUpdate 缺字段 {f}"


def test_ontology_metric_read_has_new_fields() -> None:
    names = set(OntologyMetricRead.model_fields)
    for f in ("metric_type", "status", "execution_source", "grain_keys"):
        assert f in names, f"OntologyMetricRead 缺字段 {f}"


def test_kpi_catalog_update_now_has_semantic_keywords() -> None:
    """T4/F3：semantic_keywords / match_threshold 之前只在 Create 上。"""
    names = set(KpiCatalogUpdate.model_fields)
    assert "semantic_keywords" in names
    assert "match_threshold" in names


def test_kpi_catalog_read_now_has_semantic_keywords() -> None:
    names = set(KpiCatalogRead.model_fields)
    assert "semantic_keywords" in names
    assert "match_threshold" in names


def test_kpi_catalog_has_structured_columns() -> None:
    for dto in (KpiCatalogCreate, KpiCatalogUpdate, KpiCatalogRead):
        names = set(dto.model_fields)
        for f in ("grain_keys", "numerator_columns", "denominator_columns"):
            assert f in names, f"{dto.__name__} 缺字段 {f}"


def test_camel_case_alias_on_measure_field() -> None:
    dto = OntologyMetricCreate(
        metricName="m",
        formula="f",
        numeratorMeasure={"agg": "SUM"},
    )
    assert dto.numerator_measure is not None
    assert dto.numerator_measure.agg == "SUM"


def test_measure_spec_schema_accepts_minimal() -> None:
    m = MeasureSpecSchema(agg="SUM")
    assert m.agg == "SUM"
    assert m.expr is None
    assert m.filter is None


def test_measure_spec_schema_rejects_bad_agg() -> None:
    with pytest.raises(ValidationError):
        MeasureSpecSchema(agg="NOT_AN_AGG")


def test_measure_spec_schema_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        MeasureSpecSchema(agg="SUM", bogus=1)


def test_kpi_catalog_update_match_threshold_range() -> None:
    with pytest.raises(ValidationError):
        KpiCatalogUpdate(match_threshold=1.5)
    with pytest.raises(ValidationError):
        KpiCatalogUpdate(match_threshold=-0.1)
