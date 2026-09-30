"""spec 构建器：决策 + 信号 + 计划 → 标准化 ChartSpec。

**标签从哪来**：本体实测 `property_alias` 填充 0/3642 行、`business_aliases` 0 行
（plan 的 F5），所以**拿不到中文属性名**。可用的代码可得串只有三处：
`plan.aggregations[].alias`（LLM 已写好的中文别名，如「占比」）、`plan.interpretation`
（LLM 已写好的中文概述，可作标题）、列名本身。故标题优先取 interpretation，
指标标签优先取 alias，其余用列名。

**为什么不让 LLM 填 spec**：见 `chart_spec.py` 的模块 docstring —— 这些字段都能
从 plan 与列名确定性派生，让 LLM 写只会引入「引用不存在的列」这类需要再校验的
失败面。这里的产出必须**恒可通过 `validateSpec`**（否则会被降级成表格，白忙一场）。
"""

from __future__ import annotations

import pytest

from app.domain.chart_spec import validateSpec
from app.domain.enums import ChartType
from app.domain.query_plan import Aggregation, QueryPlan, SortSpec
from app.services.chart_decision import buildChartSignals, decideChartKind
from app.services.chart_spec_builder import buildSpec
from app.services.chart_thresholds import ChartThresholds

_THRESHOLDS = ChartThresholds(
    pieMaxRows=6, hbarMinRows=15, heatmapMinCoverage=0.6, topNMax=20
)

_ROWS = [
    {"SUPPLIER_NAME": "B125 浙江力航", "RCV_QTY_PUU": 9812, "LINE_AMT": 98120},
    {"SUPPLIER_NAME": "B019 温州圣特", "RCV_QTY_PUU": 7401, "LINE_AMT": 74010},
    {"SUPPLIER_NAME": "B153 天津精一", "RCV_QTY_PUU": 5203, "LINE_AMT": 52030},
]
_MONTH_ROWS = [
    {"MONTH": "2026-01", "LINE_AMT": 100},
    {"MONTH": "2026-02", "LINE_AMT": 200},
    {"MONTH": "2026-03", "LINE_AMT": 150},
]
_CROSS_ROWS = [
    {"SUPPLIER_NAME": "S1", "MATERIAL_NAME": "M1", "AMT": 10},
    {"SUPPLIER_NAME": "S1", "MATERIAL_NAME": "M2", "AMT": 20},
    {"SUPPLIER_NAME": "S2", "MATERIAL_NAME": "M1", "AMT": 30},
    {"SUPPLIER_NAME": "S2", "MATERIAL_NAME": "M2", "AMT": 40},
]


def _plan(**overrides) -> QueryPlan:
    base: dict = {"target": "DWD_GOODS_RECEIPT_DTL"}
    base.update(overrides)
    return QueryPlan(**base)


def _build(columns: list[str], rows: list[dict], *, plan: QueryPlan | None = None, question: str = ""):
    """跑完整链路（信号 → 决策 → spec），返回 (spec, signals)。"""
    plan = plan if plan is not None else _plan()
    signals = buildChartSignals(plan, columns, rows, question)
    decision = decideChartKind(signals, _THRESHOLDS)
    return buildSpec(decision, signals, rows, plan), signals


class TestSpecIsAlwaysValid:
    """构建器的第一职责：产出必须过校验，否则渲染前就被降级成表格。"""

    @pytest.mark.parametrize(
        "columns,rows,plan,question",
        [
            (["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS, None, ""),
            (
                ["SUPPLIER_NAME", "占比"],
                [{"SUPPLIER_NAME": "B125", "占比": 0.6}, {"SUPPLIER_NAME": "B019", "占比": 0.4}],
                _plan(
                    aggregations=(
                        Aggregation(function="SUM", property="RCV_QTY_PUU", alias="占比", formula="x"),
                    ),
                    groupBy=("SUPPLIER_NAME",),
                ),
                "",
            ),
            (["MONTH", "LINE_AMT"], _MONTH_ROWS, _plan(groupBy=("MONTH",)), ""),
            (
                ["SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"],
                _ROWS,
                None,
                "供货量和金额有什么关系",
            ),
            (
                ["SUPPLIER_NAME", "MATERIAL_NAME", "AMT"],
                _CROSS_ROWS,
                _plan(
                    groupBy=("SUPPLIER_NAME", "MATERIAL_NAME"),
                    aggregations=(Aggregation(function="SUM", property="AMT"),),
                ),
                "",
            ),
            (["RCV_QTY_PUU"], [{"RCV_QTY_PUU": 22416}], None, ""),
            (
                ["PO_NO", "SUPPLIER_NAME", "LINE_AMT"],
                [{"PO_NO": "P1", "SUPPLIER_NAME": "B125", "LINE_AMT": 10}],
                None,
                "",
            ),
        ],
    )
    def test_every_shape_produces_a_spec_that_validates(
        self, columns: list[str], rows: list[dict], plan: QueryPlan | None, question: str
    ) -> None:
        spec, _ = _build(columns, rows, plan=plan, question=question)

        reason = validateSpec(spec, columns)
        assert reason is None, f"构建出的 spec 不合法：{reason}；kind={spec.kind}"

    def test_kind_matches_the_decision(self) -> None:
        spec, signals = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS)
        decision = decideChartKind(signals, _THRESHOLDS)

        assert spec.kind is decision.kind


class TestLabels:
    def test_measure_label_prefers_the_plan_alias(self) -> None:
        plan = _plan(
            aggregations=(
                Aggregation(function="SUM", property="RCV_QTY_PUU", alias="总供货量"),
            ),
            groupBy=("SUPPLIER_NAME",),
        )
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS, plan=plan)

        assert spec.measures[0].label == "总供货量"

    def test_measure_label_falls_back_to_the_column_name(self) -> None:
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS)
        assert spec.measures[0].label  # 非空即可：本体没有中文别名可用（F5）

    def test_title_prefers_the_plan_interpretation(self) -> None:
        plan = _plan(
            interpretation="各供应商的累计供货量对比",
            aggregations=(Aggregation(function="SUM", property="RCV_QTY_PUU"),),
            groupBy=("SUPPLIER_NAME",),
        )
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS, plan=plan)

        assert spec.title == "各供应商的累计供货量对比"

    def test_title_falls_back_when_no_interpretation(self) -> None:
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS)
        assert spec.title  # 总得有个标题，不能是空串

    def test_every_referenced_field_exists_in_columns(self) -> None:
        columns = ["SUPPLIER_NAME", "RCV_QTY_PUU"]
        spec, _ = _build(columns, _ROWS)

        referenced = {d.field for d in spec.dimensions} | {m.field for m in spec.measures}
        assert referenced <= set(columns)


class TestSeriesShape:
    def test_bar_has_one_series_per_measure(self) -> None:
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS)
        assert len(spec.series) == len(spec.measures) >= 1
        assert spec.series[0].kind in {"bar", "line"}

    def test_share_kind_is_pie_like_and_has_one_series(self) -> None:
        plan = _plan(
            aggregations=(
                Aggregation(function="SUM", property="RCV_QTY_PUU", alias="占比", formula="x"),
            ),
            groupBy=("SUPPLIER_NAME",),
        )
        rows = [{"SUPPLIER_NAME": "B125", "占比": 0.6}, {"SUPPLIER_NAME": "B019", "占比": 0.4}]
        spec, _ = _build(["SUPPLIER_NAME", "占比"], rows, plan=plan)

        assert spec.kind in {ChartType.PIE, ChartType.DONUT}
        assert len(spec.series) == 1

    def test_scatter_uses_two_measures(self) -> None:
        spec, signals = _build(
            ["SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"],
            _ROWS,
            question="供货量和金额有什么关系",
        )
        if spec.kind is ChartType.SCATTER:
            assert len(spec.measures) == 2
        else:  # 规则可能判成别的（比如 2 指标 1 维度也满足柱状），但形状必须合法
            assert validateSpec(spec, signals.columns) is None

    def test_heatmap_uses_two_dimensions(self) -> None:
        plan = _plan(
            groupBy=("SUPPLIER_NAME", "MATERIAL_NAME"),
            aggregations=(Aggregation(function="SUM", property="AMT"),),
        )
        spec, _ = _build(
            ["SUPPLIER_NAME", "MATERIAL_NAME", "AMT"], _CROSS_ROWS, plan=plan
        )

        assert spec.kind is ChartType.HEATMAP
        assert len(spec.dimensions) == 2
        assert len(spec.series) == 1

    def test_combo_second_series_is_a_line_on_the_right_axis(self) -> None:
        rows = [
            {"MONTH": "2026-01", "LINE_AMT": 100, "占比": 0.4},
            {"MONTH": "2026-02", "LINE_AMT": 120, "占比": 0.6},
        ]
        plan = _plan(
            aggregations=(
                Aggregation(function="SUM", property="LINE_AMT"),
                Aggregation(function="SUM", property="LINE_AMT", alias="占比", formula="x"),
            ),
            groupBy=("MONTH",),
        )
        spec, _ = _build(["MONTH", "LINE_AMT", "占比"], rows, plan=plan)

        assert spec.kind is ChartType.COMBO
        assert spec.series[0].kind == "bar"
        assert spec.series[1].kind == "line"
        assert spec.series[1].axis == "right"


class TestKpi:
    def test_kpi_carries_the_single_value(self) -> None:
        plan = _plan(aggregations=(Aggregation(function="SUM", property="RCV_QTY_PUU", alias="总供货量"),))
        spec, _ = _build(["RCV_QTY_PUU"], [{"RCV_QTY_PUU": 22416}], plan=plan)

        assert spec.kind is ChartType.KPI
        assert spec.kpi is not None
        assert spec.kpi.value == 22416
        assert spec.kpi.label == "总供货量"


class TestSortAndFlags:
    def test_sort_is_carried_from_the_plan(self) -> None:
        plan = _plan(
            groupBy=("SUPPLIER_NAME",),
            sortBy=(SortSpec(property="RCV_QTY_PUU", direction="desc"),),
            rowLimit=3,
        )
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS, plan=plan)

        assert spec.sort is not None
        assert spec.sort.by == "RCV_QTY_PUU"
        assert spec.sort.order == "desc"

    def test_sort_is_omitted_when_the_plan_has_none(self) -> None:
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS)
        assert spec.sort is None

    def test_hbar_is_marked_horizontal(self) -> None:
        rows = [{"SUPPLIER_NAME": f"B{i:03d}", "RCV_QTY_PUU": i} for i in range(40)]
        spec, _ = _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], rows)

        assert spec.kind is ChartType.HBAR
        assert spec.orientation == "horizontal"

    def test_columns_are_carried_for_the_table_payload(self) -> None:
        columns = ["SUPPLIER_NAME", "RCV_QTY_PUU"]
        spec, _ = _build(columns, _ROWS)
        assert spec.columns == tuple(columns)


class TestAmbiguityFlipKeepsTheAxis:
    """消歧把 kind 换掉之后，轴必须还是同一根 —— 否则合法图被白降级成表格。

    真实路径：问句「按月的入库金额」（无「趋势」字样）→ 走分类器 → 答 COMPARE
    → LINE 变 BAR。这份数据只有一根轴（MONTH，时间列）。
    """

    def test_bar_flipped_from_a_time_trend_keeps_the_time_axis(self) -> None:
        plan = _plan(groupBy=("MONTH",))
        signals = buildChartSignals(plan, ["MONTH", "LINE_AMT"], _MONTH_ROWS, "")
        decision = decideChartKind(signals, _THRESHOLDS, labelHint="COMPARE")
        assert decision.kind is ChartType.BAR

        spec = buildSpec(decision, signals, _MONTH_ROWS, plan)

        assert [d.field for d in spec.dimensions] == ["MONTH"]
        assert validateSpec(spec, ["MONTH", "LINE_AMT"]) is None

    def test_hbar_flipped_from_a_category_bar_keeps_the_category_axis(self) -> None:
        plan = _plan(groupBy=("SUPPLIER_NAME",))
        signals = buildChartSignals(plan, ["SUPPLIER_NAME", "RCV_QTY_PUU"], _ROWS, "")
        decision = decideChartKind(signals, _THRESHOLDS, labelHint="RANK")
        assert decision.kind is ChartType.HBAR

        spec = buildSpec(decision, signals, _ROWS, plan)

        assert [d.field for d in spec.dimensions] == ["SUPPLIER_NAME"]
        assert spec.orientation == "horizontal"


class TestPlanIdentifiersAgainstResultColumns:
    """生产形态：plan 用本体属性名（`SUPPLIER_CODE`），结果集列名是小写。

    `business_db_pool.py:639` 对结果列统一 `c.lower()`，所以 `SUPPLIER_CODE` 与
    `supplier_code` 指的是同一列。把 plan 标识符原样抄进 spec，`validateSpec` 就会
    判定「引用了结果集中不存在的列」→ **整张图降级成表格**。

    2026-09-30 线上复现（用户强制饼图却拿到表格）：
    `rule=R_FORCED_CLIENT decision=pie：引用了结果集中不存在的列 'SUPPLIER_CODE'
    （列：['item_code', ..., 'supplier_code', ...]）`。

    既有用例全是「plan 与列名同为大写」，所以这条鸿沟一直没被测出来。
    """

    _COLUMNS = ["supplier_code", "total_qty"]
    _ROWS_LOWER = [
        {"supplier_code": "B125", "total_qty": 9812},
        {"supplier_code": "B019", "total_qty": 7401},
    ]

    def test_sort_reference_resolves_to_the_actual_column(self) -> None:
        plan = _plan(
            groupBy=("SUPPLIER_CODE",),
            sortBy=(SortSpec(property="SUPPLIER_CODE", direction="desc"),),
        )

        spec, _ = _build(self._COLUMNS, self._ROWS_LOWER, plan=plan)

        assert spec.sort is not None
        assert spec.sort.by == "supplier_code"
        assert validateSpec(spec, self._COLUMNS) is None

    def test_sort_is_dropped_when_the_reference_is_unresolvable(self) -> None:
        plan = _plan(sortBy=(SortSpec(property="NOT_IN_RESULT", direction="desc"),))

        spec, _ = _build(self._COLUMNS, self._ROWS_LOWER, plan=plan)

        # 解析不出来就丢掉排序，而不是留一个指向空列的引用把整张图拖去降级。
        assert spec.sort is None
        assert validateSpec(spec, self._COLUMNS) is None

    def test_measure_label_uses_the_plan_alias_despite_case(self) -> None:
        plan = _plan(
            groupBy=("SUPPLIER_NAME",),
            aggregations=(
                Aggregation(property="RCV_QTY_PUU", function="SUM", alias="占比"),
            ),
        )
        columns = ["supplier_name", "rcv_qty_puu"]
        rows = [
            {"supplier_name": "B125 浙江力航", "rcv_qty_puu": 9812},
            {"supplier_name": "B019 温州圣特", "rcv_qty_puu": 7401},
        ]

        spec, _ = _build(columns, rows, plan=plan)

        assert spec.measures[0].label == "占比"
        assert validateSpec(spec, columns) is None


class TestImmutability:
    def test_build_does_not_mutate_the_plan_or_rows(self) -> None:
        plan = _plan(groupBy=("SUPPLIER_NAME",))
        rows = [dict(r) for r in _ROWS]
        before = [dict(r) for r in rows]

        _build(["SUPPLIER_NAME", "RCV_QTY_PUU"], rows, plan=plan)

        assert rows == before
        assert plan.groupBy == ("SUPPLIER_NAME",)
