"""columnLabels / units 在 spec→renderer→tableOption 链路上的透传。"""

from __future__ import annotations

from app.domain.chart_spec import ChartSpec, coerceSpec, tableSpec
from app.domain.enums import ChartType
from app.services.chart_decision import ChartDecision, buildChartSignals
from app.services.chart_renderer import renderChartOption
from app.services.chart_spec_builder import buildSpec
from app.services.visual_payload import assembleTableOption

_ROWS = [
    {"supplier_code": "B1", "rcv_qty": 3},
    {"supplier_code": "B2", "rcv_qty": 5},
    {"supplier_code": "B3", "rcv_qty": 7},
]


def _signals(columns: list[str], rows: list[dict]):
    """复用生产同一派生入口，避免手搓 ChartSignals（字段多、易漂移）。"""
    return buildChartSignals(None, columns, rows, "")


def _decision(kind: ChartType) -> ChartDecision:
    return ChartDecision(kind=kind, ruleId="R_TEST", candidates=(), ambiguous=False)


def test_build_spec_appends_unit_to_measure_and_series():
    spec = buildSpec(
        _decision(ChartType.BAR),
        _signals(["supplier_code", "rcv_qty"], _ROWS),
        _ROWS,
        aliasOverrides={"supplier_code": "供应商编码", "rcv_qty": "收货数量"},
        units={"rcv_qty": "件"},
    )
    assert spec.measures[0].label == "收货数量（件）"
    assert spec.series[0].name == "收货数量（件）"
    assert spec.dimensions[0].label == "供应商编码"
    assert spec.columnLabels == {
        "supplier_code": "供应商编码",
        "rcv_qty": "收货数量（件）",
    }


def test_build_spec_omits_labels_when_unresolved():
    rows = [{"supplier_name": "甲", "total_qty": 3}]
    spec = buildSpec(
        _decision(ChartType.BAR),
        _signals(["supplier_name", "total_qty"], rows),
        rows,
    )
    assert spec.columnLabels == {}
    assert spec.measures[0].label == "total qty"


def test_table_spec_carries_labels():
    spec = tableSpec(["a", "b"], columnLabels={"a": "甲"})
    assert spec.columnLabels == {"a": "甲"}


def test_coerce_degrade_preserves_labels():
    spec = ChartSpec(
        kind=ChartType.BAR,
        columns=("a",),
        columnLabels={"a": "甲"},
        measures=(),
    )
    coerced, reason = coerceSpec(spec, ["a"])
    assert reason is not None
    assert coerced.kind is ChartType.TABLE
    assert coerced.columnLabels == {"a": "甲"}


def test_render_table_includes_column_labels():
    spec = tableSpec(["a"], columnLabels={"a": "甲（件）"})
    option = renderChartOption(spec, [{"a": 1}])
    assert option["columnLabels"] == {"a": "甲（件）"}


def test_render_table_omits_empty_labels():
    option = renderChartOption(tableSpec(["a"]), [{"a": 1}])
    assert "columnLabels" not in option


def test_assemble_table_option_passes_labels():
    payload = assembleTableOption(
        specKind=ChartType.BAR,
        columns=["a"],
        data=[{"a": 1}],
        fullDataThreshold=100,
        columnLabels={"a": "甲"},
    )
    assert payload is not None
    assert payload["columnLabels"] == {"a": "甲"}
