"""columnLabels / units 在 spec→renderer→tableOption 链路上的透传。"""

from __future__ import annotations

from app.domain.chart_spec import ChartSpec, coerceSpec, tableSpec
from app.domain.enums import ChartType
from app.domain.query_plan import Aggregation, QueryPlan
from app.services.chart_decision import ChartDecision, buildChartSignals
from app.services.chart_renderer import renderChartOption
from app.services.chart_spec_builder import buildSpec
from app.services.field_display_resolver import (
    SOURCE_ONTOLOGY_ALIAS,
    SOURCE_PLAN_ALIAS,
    FieldDisplay,
    displayMaps,
    missingFieldHints,
)
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


def _countOnDimensionPlan() -> QueryPlan:
    """合法计划形状：COUNT(维度列) 的别名会被 aliasByField 挂到该维度列上。"""
    return QueryPlan(
        target="DWD_GOODS_RECEIPT_DTL",
        groupBy=("SUPPLIER_NAME",),
        aggregations=(Aggregation(function="COUNT", property="SUPPLIER_NAME", alias="记录数"),),
    )


def _countOnDimensionRows() -> list[dict]:
    return [
        {"supplier_name": "B125", "记录数": 12},
        {"supplier_name": "B019", "记录数": 7},
    ]


def test_dimension_header_ignores_plan_aggregation_alias():
    """维度列的表头只吃注入的 overrides，不吃 plan 聚合别名（否则两列都叫「记录数」）。"""
    plan = _countOnDimensionPlan()
    rows = _countOnDimensionRows()
    columns = ["supplier_name", "记录数"]
    spec = buildSpec(
        _decision(ChartType.BAR),
        buildChartSignals(plan, columns, rows, ""),
        rows,
        plan,
    )
    assert "supplier_name" not in spec.columnLabels


def test_dimension_header_still_uses_injected_override():
    """同一形状上，显式注入的 overrides 仍然给维度列表头（能力不缩水）。"""
    plan = _countOnDimensionPlan()
    rows = _countOnDimensionRows()
    columns = ["supplier_name", "记录数"]
    spec = buildSpec(
        _decision(ChartType.BAR),
        buildChartSignals(plan, columns, rows, ""),
        rows,
        plan,
        aliasOverrides={"supplier_name": "供应商"},
    )
    assert spec.columnLabels["supplier_name"] == "供应商"


def test_units_do_not_double_suffix_labels():
    """别名已带「（单位）」时不重复拼接。"""
    spec = buildSpec(
        _decision(ChartType.BAR),
        _signals(["supplier_code", "rcv_qty"], _ROWS),
        _ROWS,
        aliasOverrides={"rcv_qty": "收货数量（件）"},
        units={"rcv_qty": "件"},
    )
    assert spec.measures[0].label == "收货数量（件）"
    assert spec.columnLabels["rcv_qty"] == "收货数量（件）"


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


def test_display_maps_excludes_plan_aggregation_alias_labels():
    """plan 聚合别名不得进 override：否则 COUNT(维度列) 会改维度表头 + 吞掉缺名提示。"""
    displays = {
        "supplier_name": FieldDisplay(
            label="记录数", unit="次", isNumeric=False, source=SOURCE_PLAN_ALIAS,
        ),
        "rcv_qty": FieldDisplay(
            label="收货数量", unit="件", isNumeric=True, source=SOURCE_ONTOLOGY_ALIAS,
        ),
    }
    labels, units = displayMaps(displays)
    assert labels == {"rcv_qty": "收货数量"}  # plan 聚合别名被丢弃
    assert units == {"supplier_name": "次", "rcv_qty": "件"}  # 单位映射不受影响


def test_missing_field_hints_reports_plan_alias_only_label():
    """只有 plan 聚合别名的列没有真正的中文含义 ⇒ 进缺名提示（表头回落原始列名）。"""
    displays = {
        "supplier_name": FieldDisplay(
            label="记录数", unit=None, isNumeric=False, source=SOURCE_PLAN_ALIAS,
        ),
    }
    hints = missingFieldHints({}, displays)
    assert hints["missingLabels"] == ["supplier_name"]


def test_missing_field_hints_skips_plan_alias_with_column_label():
    """同一 display 若已在 columnLabels 里（指标经 aliasByField 拿到标签）⇒ 不报缺名。"""
    displays = {
        "supplier_name": FieldDisplay(
            label="记录数", unit=None, isNumeric=False, source=SOURCE_PLAN_ALIAS,
        ),
    }
    hints = missingFieldHints({"supplier_name": "记录数"}, displays)
    assert hints["missingLabels"] == []
