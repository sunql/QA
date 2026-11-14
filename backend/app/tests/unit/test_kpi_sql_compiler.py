"""measure → SQL 编译器矩阵（纯函数，不连库）。"""

from __future__ import annotations

import pytest

from app.domain.models import OntologyClass, OntologyJoin, OntologyMetric, OntologyMetricSource
from app.services.kpi_sql_compiler import compileMetricSql


def _metric(**overrides) -> OntologyMetric:
    base = dict(
        id=1,
        metric_name="m",
        formula="",
        metric_type="MEASURE",
        numerator_measure={"agg": "SUM", "expr": "AMOUNT", "class_id": 10},
        target_class_id=10,
        execution_source="COMPILED",
    )
    base.update(overrides)
    return OntologyMetric(**base)


def _cls(id: int, table: str) -> OntologyClass:
    return OntologyClass(id=id, class_name=f"c{id}", source_table=table)


def _compile(metric, classes, joins=(), sources=(), dialect="postgresql", **kw):
    return compileMetricSql(
        metric,
        sources=sources,
        classes=classes,
        joins=joins,
        datasourceType=dialect,
        **kw,
    )


def _monthScope():
    """TimeScope 实际形状（time_scope_resolver）：半开区间 [start, end)。"""
    from datetime import date

    from app.services.time_scope_renderer import TimeScope

    return TimeScope(
        matched="2026年7月",
        start=date(2026, 7, 1),
        end=date(2026, 8, 1),
        label="2026年7月",
        granularity="month",
    )


def test_measure_sum_compiles_single_value_select() -> None:
    result = _compile(_metric(), [_cls(10, "orders")])

    assert result.ok is True
    assert result.sql == 'SELECT SUM("AMOUNT") AS value FROM "orders"'
    assert result.reason is None


def test_count_without_expr_uses_count_star() -> None:
    metric = _metric(numerator_measure={"agg": "COUNT", "expr": None, "class_id": 10})

    assert _compile(metric, [_cls(10, "orders")]).sql == 'SELECT COUNT(*) AS value FROM "orders"'


def test_count_distinct_requires_expr() -> None:
    metric = _metric(numerator_measure={"agg": "COUNT_DISTINCT", "expr": None, "class_id": 10})

    result = _compile(metric, [_cls(10, "orders")])

    assert result.ok is False
    assert result.reason == "compiled_expr_missing"


def test_weighted_ratio_sums_then_divides_with_nullif() -> None:
    metric = _metric(
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        numerator_measure={"agg": "SUM", "expr": "OK_QTY", "class_id": 10},
        denominator_measure={"agg": "SUM", "expr": "TOTAL_QTY", "class_id": 10},
    )

    sql = _compile(metric, [_cls(10, "orders")]).sql

    assert sql == (
        'SELECT SUM("OK_QTY") / NULLIF(SUM("TOTAL_QTY"), 0) AS value FROM "orders"'
    )


def test_avg_of_ratios_divides_per_row_then_averages() -> None:
    metric = _metric(
        metric_type="RATIO",
        aggregation_order="AVG_OF_RATIOS",
        numerator_measure={"agg": "SUM", "expr": "OK_QTY", "class_id": 10},
        denominator_measure={"agg": "SUM", "expr": "TOTAL_QTY", "class_id": 10},
    )

    sql = _compile(metric, [_cls(10, "orders")]).sql

    assert sql == (
        'SELECT AVG("OK_QTY" / NULLIF("TOTAL_QTY", 0)) AS value FROM "orders"'
    )


def test_difference_subtracts_denominator() -> None:
    metric = _metric(
        metric_type="DIFFERENCE",
        numerator_measure={"agg": "SUM", "expr": "A", "class_id": 10},
        denominator_measure={"agg": "SUM", "expr": "B", "class_id": 10},
    )

    sql = _compile(metric, [_cls(10, "orders")]).sql

    assert sql == 'SELECT SUM("A") - SUM("B") AS value FROM "orders"'


def test_measure_filter_becomes_case_when_not_where() -> None:
    metric = _metric(
        numerator_measure={
            "agg": "SUM",
            "expr": "AMOUNT",
            "filter": "STATUS = 'OK'",
            "class_id": 10,
        }
    )

    sql = _compile(metric, [_cls(10, "orders")]).sql

    assert sql == (
        "SELECT SUM(CASE WHEN STATUS = 'OK' THEN \"AMOUNT\" END) AS value "
        'FROM "orders"'
    )
    assert "WHERE" not in sql


def test_global_filter_goes_to_where() -> None:
    metric = _metric(filter_expression="REGION = 'EAST'")

    sql = _compile(metric, [_cls(10, "orders")]).sql

    assert sql == 'SELECT SUM("AMOUNT") AS value FROM "orders" WHERE REGION = \'EAST\''


def test_cross_table_measure_adds_join_from_catalog() -> None:
    metric = _metric(
        numerator_measure={"agg": "SUM", "expr": "QTY", "class_id": 20},
        denominator_measure={"agg": "SUM", "expr": "QTY", "class_id": 10},
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
    )
    join = OntologyJoin(
        source_class_id=20,
        source_columns=["ORDER_ID"],
        target_class_id=10,
        target_columns=["ID"],
        join_type="INNER",
    )

    result = _compile(
        metric,
        [_cls(10, "orders"), _cls(20, "order_lines")],
        [join],
    )

    assert result.ok is True
    assert '"order_lines"' in result.sql
    assert 'JOIN "orders" ON "order_lines"."ORDER_ID" = "orders"."ID"' in result.sql


def test_no_join_path_fails_closed() -> None:
    metric = _metric(
        numerator_measure={"agg": "SUM", "expr": "QTY", "class_id": 20},
        denominator_measure={"agg": "SUM", "expr": "QTY", "class_id": 10},
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
    )

    result = _compile(metric, [_cls(10, "orders"), _cls(20, "order_lines")])

    assert result.ok is False
    assert result.reason == "compiled_join_path_missing"


@pytest.mark.parametrize(
    "fragment",
    ["AMOUNT; DROP TABLE users", "AMOUNT -- x", "AMOUNT /* x */", "AMOUNT) OR (1=1"],
)
def test_unsafe_fragments_fail_closed(fragment: str) -> None:
    metric = _metric(numerator_measure={"agg": "SUM", "expr": fragment, "class_id": 10})

    result = _compile(metric, [_cls(10, "orders")])

    assert result.ok is False
    assert result.reason == "compiled_invalid_fragment"


def test_measure_without_class_id_falls_back_to_unique_source() -> None:
    # measure 里没写 class_id，但全指标只有一个源类 ⇒ 回退该源类（不能 fail）
    metric = _metric(
        numerator_measure={"agg": "SUM", "expr": "AMOUNT"},
        target_class_id=None,
    )
    sources = [OntologyMetricSource(metric_id=1, class_id=10, role="BASE")]

    result = _compile(metric, [_cls(10, "orders")], sources=sources)

    assert result.ok is True
    assert 'FROM "orders"' in result.sql


def test_measure_without_class_id_and_multiple_sources_fails_closed() -> None:
    metric = _metric(
        numerator_measure={"agg": "SUM", "expr": "AMOUNT"},
        target_class_id=None,
    )
    sources = [
        OntologyMetricSource(metric_id=1, class_id=10, role="BASE"),
        OntologyMetricSource(metric_id=1, class_id=20, role="FILTER"),
    ]

    result = _compile(metric, [_cls(10, "orders"), _cls(20, "order_lines")], sources=sources)

    assert result.ok is False
    assert result.reason == "compiled_measure_class_unresolved"


def test_derived_metric_type_fails_closed() -> None:
    result = _compile(_metric(metric_type="DERIVED"), [_cls(10, "orders")])

    assert result.ok is False
    assert result.reason == "compiled_metric_type_unsupported"


def test_unknown_dialect_fails_closed() -> None:
    result = _compile(_metric(), [_cls(10, "orders")], dialect="sqlite")

    assert result.ok is False
    assert result.reason == "compiled_dialect_unsupported"


def test_mysql_quotes_identifiers_with_backticks() -> None:
    sql = _compile(_metric(), [_cls(10, "orders")], dialect="mysql").sql

    assert sql == "SELECT SUM(`AMOUNT`) AS value FROM `orders`"


def test_grain_keys_ignored_produces_warning_but_still_compiles() -> None:
    metric = _metric(grain_keys=["REGION"])

    result = _compile(metric, [_cls(10, "orders")])

    assert result.ok is True
    assert result.warnings == ("grain_keys_ignored",)


def test_oracle_time_scope_renders_predicate() -> None:
    metric = _metric(time_grain="month", time_column="ORDER_DATE")
    sql = _compile(
        metric,
        [_cls(10, "orders")],
        dialect="oracle",
        timeScope=_monthScope(),
    ).sql

    assert "ORDER_DATE" in sql
    # time_scope_renderer 用 ANSI DATE 字面量（半开区间），非 TO_DATE
    assert "DATE '2026-07-01'" in sql
    assert "DATE '2026-08-01'" in sql


def test_postgres_time_scope_fails_closed() -> None:
    metric = _metric(time_grain="month", time_column="ORDER_DATE")
    result = _compile(
        metric,
        [_cls(10, "orders")],
        dialect="postgresql",
        timeScope=_monthScope(),
    )

    assert result.ok is False
    assert result.reason == "compiled_time_scope_unsupported"
