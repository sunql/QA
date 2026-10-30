"""KPI 指标技术形态 —— 枚举与 ORM 模型形状（纯元数据，不碰库）。"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.domain.enums import (
    MetricAggregationOrder,
    MetricExecutionSource,
    MetricSourceRole,
    MetricStatus,
    MetricType,
)
from app.domain.models import KpiCatalog, OntologyMetric, OntologyMetricSource


def test_metric_type_enum_values() -> None:
    assert [e.value for e in MetricType] == [
        "MEASURE",
        "RATIO",
        "DIFFERENCE",
        "DERIVED",
    ]


def test_metric_aggregation_order_enum_values() -> None:
    assert [e.value for e in MetricAggregationOrder] == [
        "WEIGHTED_RATIO",
        "AVG_OF_RATIOS",
        "NONE",
    ]


def test_metric_execution_source_enum_values() -> None:
    assert [e.value for e in MetricExecutionSource] == [
        "COMPILED",
        "ESCAPE_HATCH",
        "NONE",
    ]


def test_metric_status_enum_values() -> None:
    assert [e.value for e in MetricStatus] == ["DRAFT", "PUBLISHED"]


def test_metric_source_role_enum_values() -> None:
    assert [e.value for e in MetricSourceRole] == [
        "NUMERATOR",
        "DENOMINATOR",
        "FILTER",
        "BASE",
    ]


def test_ontology_metric_has_structured_columns() -> None:
    cols = {c.name for c in OntologyMetric.__table__.columns}
    for name in (
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
        assert name in cols, f"ontology_metric 缺列 {name}"


def test_ontology_metric_source_table_shape() -> None:
    table = OntologyMetricSource.__table__
    assert table.name == "ontology_metric_source"
    cols = {c.name for c in table.columns}
    assert cols == {"id", "metric_id", "class_id", "role"}


def test_ontology_metric_source_fk_ondelete() -> None:
    fks = {
        (fk.parent.name, fk.column.name, fk.ondelete)
        for fk in OntologyMetricSource.__table__.foreign_keys
    }
    assert ("metric_id", "id", "CASCADE") in fks
    assert ("class_id", "id", "RESTRICT") in fks


def test_kpi_catalog_has_structured_columns() -> None:
    cols = {c.name for c in KpiCatalog.__table__.columns}
    for name in ("grain_keys", "numerator_columns", "denominator_columns"):
        assert name in cols, f"kpi_catalog 缺列 {name}"


def test_metric_type_column_default_is_measure() -> None:
    col = OntologyMetric.__table__.columns["metric_type"]
    assert col.default.arg == "MEASURE"
    assert col.server_default is not None
    assert col.server_default.arg == "MEASURE"


def test_metric_status_column_default_is_draft() -> None:
    col = OntologyMetric.__table__.columns["status"]
    assert col.default.arg == "DRAFT"
    assert col.server_default is not None
    assert col.server_default.arg == "DRAFT"


def test_json_columns_use_jsonb_variant() -> None:
    """JSON 列必须 JSON→JSONB variant（PG 侧是 JSONB）。"""
    for table, name in (
        (OntologyMetric, "numerator_measure"),
        (OntologyMetric, "denominator_measure"),
        (OntologyMetric, "grain_keys"),
        (KpiCatalog, "grain_keys"),
        (KpiCatalog, "numerator_columns"),
        (KpiCatalog, "denominator_columns"),
    ):
        col = table.__table__.columns[name]
        # with_variant 不体现在 str(基类型) 上，必须按 PG 方言解析 impl 才见真身
        impl = col.type.dialect_impl(postgresql.dialect())
        assert "JSONB" in str(impl), f"{table.__tablename__}.{name} 不是 JSONB variant"


@pytest.mark.parametrize("col_name", ["numerator_measure", "denominator_measure", "grain_keys"])
def test_ontology_metric_json_columns_nullable(col_name: str) -> None:
    assert OntologyMetric.__table__.columns[col_name].nullable is True
