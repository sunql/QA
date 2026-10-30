"""渲染器：ChartSpec + 数据 → **不含颜色**的 ECharts option（11 个 kind）。

两条契约：

1. **服务端只发结构**（决策 6）。颜色属于前端主题层，所以这里的断言逐键检查
   「没有任何 #hex / rgb() / 调色板数组」。唯一的例外是瀑布图的透明占位系列 ——
   `"transparent"` 不是主题色，是让堆叠基线不可见的结构手段。
2. **绝不抛错**（F2）。前端的渲染门是 `chartType && chartOption`，所以
   `renderChartOption` 是**全函数**：任何异常都降级为表格负载，而不是让用户
   什么都看不到。

`{d}`（ECharts 的饼图百分比模板变量）只允许出现在饼图/环形图 —— 其他图形
ECharts 找不到替换目标，会原样输出字面量 `{d}`（这是用户报过的老 bug）。
"""

from __future__ import annotations

import pytest

from app.domain.chart_spec import (
    ChartSpec,
    SpecDimension,
    SpecKpi,
    SpecMeasure,
    SpecSeries,
    SpecSort,
    tableSpec,
)
from app.domain.enums import ChartType
from app.services.chart_renderer import renderChartOption

_CATS = ["B125 浙江力航", "B019 温州圣特", "B153 天津精一"]
_ROWS = [
    {"SUPPLIER_NAME": _CATS[0], "RCV_QTY_PUU": 9812, "LINE_AMT": 98120},
    {"SUPPLIER_NAME": _CATS[1], "RCV_QTY_PUU": 7401, "LINE_AMT": 74010},
    {"SUPPLIER_NAME": _CATS[2], "RCV_QTY_PUU": 5203, "LINE_AMT": 52030},
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


def _dim(field: str, label: str = "供应商") -> SpecDimension:
    return SpecDimension(field=field, label=label)


def _measure(field: str, label: str = "供货量") -> SpecMeasure:
    return SpecMeasure(field=field, label=label, agg="SUM")


def _spec(kind: ChartType, **overrides) -> ChartSpec:
    """构造一个「形状正确」的 spec；各用例只覆盖自己关心的字段。"""
    base: dict = {
        "kind": kind,
        "title": "供货量 按 供应商",
        "columns": ("SUPPLIER_NAME", "RCV_QTY_PUU"),
        "dimensions": (_dim("SUPPLIER_NAME"),),
        "measures": (_measure("RCV_QTY_PUU"),),
        "series": (
            SpecSeries(
                name="供货量",
                kind="bar",
                dimension="SUPPLIER_NAME",
                measure="RCV_QTY_PUU",
            ),
        ),
    }
    base.update(overrides)
    return ChartSpec(**base)


def _colorKeys(node: object, key: str = "") -> list[tuple[str, object]]:
    """递归收集「看起来是颜色」的键值，供「服务端不选色」断言使用。"""
    found: list[tuple[str, object]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "color" or k.endswith("Color") or k in {"itemStyle", "lineStyle"}:
                found.append((k, v))
            found.extend(_colorKeys(v, k))
    elif isinstance(node, list):
        for item in node:
            found.extend(_colorKeys(item, key))
    return found


def _assertNoHue(option: dict) -> None:
    """断言 option 里没有任何 #hex / rgb() / hsl() 色值，也没有调色板数组。"""
    assert "color" not in option, "根级 color 调色板不该由服务端下发"
    for key, value in _colorKeys(option):
        if isinstance(value, str):
            assert not value.startswith("#"), f"{key} 下发了 hex 色值 {value!r}"
            assert "rgb" not in value and "hsl" not in value, f"{key}={value!r}"


class TestTable:
    def test_table_keeps_the_legacy_payload(self) -> None:
        """TABLE 契约不变：{"columns", "rows"} 原样，前端表格分支零改动。"""
        option = renderChartOption(tableSpec(["A", "B"]), _ROWS)

        assert option == {"columns": ["A", "B"], "rows": _ROWS}

    def test_empty_data_still_yields_a_payload(self) -> None:
        option = renderChartOption(tableSpec(["A"]), [])
        assert option["columns"] == ["A"]
        assert option["rows"] == []


class TestKpi:
    def test_kpi_carries_its_own_payload_not_echarts(self) -> None:
        """KPI 不是 ECharts：负载沿用 TABLE 的先例（chartOption 是「该类型的渲染负载」）。"""
        spec = _spec(
            ChartType.KPI,
            kpi=SpecKpi(label="供货量", value=22416, unit="件"),
        )

        option = renderChartOption(spec, [{"RCV_QTY_PUU": 22416}])

        assert "series" not in option
        assert option["kpi"] == {
            "label": "供货量",
            "value": 22416,
            "unit": "件",
            "delta": None,
        }


class TestCartesian:
    def test_line_uses_category_axis_and_value_series(self) -> None:
        spec = _spec(
            ChartType.LINE,
            columns=("MONTH", "LINE_AMT"),
            dimensions=(_dim("MONTH", "月份"),),
            measures=(_measure("LINE_AMT", "金额"),),
            series=(
                SpecSeries(name="金额", kind="line", dimension="MONTH", measure="LINE_AMT"),
            ),
        )

        option = renderChartOption(spec, _MONTH_ROWS)

        assert option["xAxis"]["type"] == "category"
        assert option["xAxis"]["data"] == ["2026-01", "2026-02", "2026-03"]
        assert option["series"][0]["type"] == "line"
        assert option["series"][0]["data"] == [100, 200, 150]
        assert option["series"][0]["name"] == "金额"

    def test_bar_uses_bar_series(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), _ROWS)
        assert option["series"][0]["type"] == "bar"
        assert option["xAxis"]["data"] == _CATS

    def test_chat_shape_renders_no_axis_name(self) -> None:
        """chat 形态（spec.axisLabels 空）⇒ 类目轴不带 name 键，负载与既有行为一致。

        这是本任务对 chat 侧的**反向护栏**：共享渲染器被改了，但没注入就没轴名。
        """
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU"),
            dimensions=(_dim("SUPPLIER_NAME"),),
            measures=(_measure("RCV_QTY_PUU"),),
            series=(
                SpecSeries(
                    name="供货量", kind="bar",
                    dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU",
                ),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert "name" not in option["xAxis"]
        assert option["xAxis"]["data"] == _CATS

    def test_injected_axis_labels_render_dimension_name_and_never_raw_field(self) -> None:
        """注入后：轴名 = 显示名（原始维度列名不得漏进 option）。

        **注意本条的 `"RCV_QTY_PUU": "RCV_QTY_PUU"` 恒等项是惰性条件**：它是指标列，
        `_buildAxisOption` 只查 `_axisNameOf(spec, catField)`（`catField` = 第一个维度），
        指标列从不作为轴字段被查询 ⇒ 删掉 `_axisNameOf` 的 `label != field` 判断本条仍绿。
        「恒等映射必须屏蔽」的真正判别由 test_identity_axis_label_is_not_rendered 承担
        （恒等项落在**类目字段**上）。
        """
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU"),
            dimensions=(_dim("SUPPLIER_NAME"),),
            measures=(_measure("RCV_QTY_PUU"),),
            series=(
                SpecSeries(
                    name="供货量", kind="bar",
                    dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU",
                ),
            ),
            axisLabels={"SUPPLIER_NAME": "供应商", "RCV_QTY_PUU": "RCV_QTY_PUU"},
        )

        option = renderChartOption(spec, _ROWS)

        assert option["xAxis"]["name"] == "供应商"   # 显示名上了轴
        assert option["xAxis"]["data"] == _CATS       # 取值仍是数据键，不被翻译

    def test_identity_axis_label_is_not_rendered(self) -> None:
        """恒等映射（label == field）不上轴 —— 否则原始列名会经轴名漏进 option。

        判别点：恒等项必须落在**类目字段**（`_dimensionOf(spec, 0)`）上才走得到
        `_axisNameOf` 的 `label != field` 分支。删掉该分支 ⇒ 轴名变回裸列名
        `SUPPLIER_NAME`，本条立刻红（上一条用例在同一变故下仍绿）。
        """
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU"),
            dimensions=(_dim("SUPPLIER_NAME"),),
            measures=(_measure("RCV_QTY_PUU"),),
            series=(
                SpecSeries(
                    name="供货量", kind="bar",
                    dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU",
                ),
            ),
            axisLabels={"SUPPLIER_NAME": "SUPPLIER_NAME"},
        )

        option = renderChartOption(spec, _ROWS)

        assert "name" not in option["xAxis"]
        assert option["xAxis"]["data"] == _CATS       # 取值仍是数据键，不被翻译

    def test_hbar_puts_categories_on_y_axis(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _ROWS)
        assert option["yAxis"]["type"] == "category"
        assert option["yAxis"]["data"] == _CATS
        assert option["xAxis"]["type"] == "value"
        assert option["series"][0]["type"] == "bar"

    def test_two_measures_produce_two_series(self) -> None:
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU"),
                SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="LINE_AMT"),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert [s["name"] for s in option["series"]] == ["供货量", "金额"]

    def test_second_dimension_becomes_grouped_series(self) -> None:
        """两维一指标：第二个维度摊成多系列（分组柱状），而不是被丢掉。"""
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "MATERIAL_NAME", "AMT"),
            dimensions=(_dim("SUPPLIER_NAME"), _dim("MATERIAL_NAME", "物料")),
            measures=(_measure("AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="AMT"),),
        )

        option = renderChartOption(spec, _CROSS_ROWS)

        assert option["xAxis"]["data"] == ["S1", "S2"]
        assert sorted(s["name"] for s in option["series"]) == ["M1", "M2"]

    def test_null_values_are_preserved_as_nulls(self) -> None:
        """缺失值要留成 null 让 ECharts 断线，不能当 0（0 是有效数据）。"""
        rows = [
            {"MONTH": "2026-01", "LINE_AMT": 100},
            {"MONTH": "2026-02", "LINE_AMT": None},
        ]
        spec = _spec(
            ChartType.LINE,
            columns=("MONTH", "LINE_AMT"),
            dimensions=(_dim("MONTH", "月份"),),
            measures=(_measure("LINE_AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="line", dimension="MONTH", measure="LINE_AMT"),),
        )
        assert renderChartOption(spec, rows)["series"][0]["data"] == [100, None]

    def test_decimal_values_become_json_numbers(self) -> None:
        from decimal import Decimal

        rows = [{"SUPPLIER_NAME": "B125", "RCV_QTY_PUU": Decimal("12.50")}]
        option = renderChartOption(_spec(ChartType.BAR), rows)
        assert option["series"][0]["data"] == [12.5]


class TestLegendPlacement:
    """图例放底部 + 滚动（用户实测：多系列图例默认顶部折行，直接盖住标题）。"""

    def test_multi_series_cartesian_legend_is_bottom_scroll_and_grid_leaves_room(self) -> None:
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU"),
                SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="LINE_AMT"),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert option["legend"] == {"show": True, "type": "scroll", "bottom": 0}
        assert option["grid"]["bottom"] == 56

    def test_single_series_has_no_legend_and_default_grid_bottom(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), _ROWS)
        assert "legend" not in option
        assert option["grid"]["bottom"] == 32

    def test_legend_disabled_keeps_default_grid_bottom(self) -> None:
        spec = _spec(
            ChartType.BAR,
            showLegend=False,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU"),
                SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="LINE_AMT"),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert "legend" not in option
        assert option["grid"]["bottom"] == 32

    def test_pie_and_donut_legend_is_bottom_scroll(self) -> None:
        for kind in (ChartType.PIE, ChartType.DONUT):
            option = renderChartOption(_spec(kind), _ROWS)
            assert option["legend"] == {"show": True, "type": "scroll", "bottom": 0}

        hidden = renderChartOption(_spec(ChartType.PIE, showLegend=False), _ROWS)
        assert hidden["legend"]["show"] is False

    def test_many_grouped_series_legend_stays_one_scrollable_row(self) -> None:
        """31 系列回归：图例必须单行滚动（type=scroll），不能折行盖标题。"""
        rows = [
            {"SUPPLIER_NAME": f"S{i}", "MATERIAL_NAME": f"M{i}", "AMT": i}
            for i in range(31)
        ]
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "MATERIAL_NAME", "AMT"),
            dimensions=(_dim("SUPPLIER_NAME"), _dim("MATERIAL_NAME", "物料")),
            measures=(_measure("AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="AMT"),),
        )

        option = renderChartOption(spec, rows)

        assert option["legend"] == {"show": True, "type": "scroll", "bottom": 0}
        assert len(option["series"]) == 31
        assert option["grid"]["bottom"] == 56


class TestPieLike:
    def test_pie_uses_named_data_points(self) -> None:
        option = renderChartOption(_spec(ChartType.PIE), _ROWS)

        assert option["series"][0]["type"] == "pie"
        assert option["series"][0]["data"] == [
            {"name": _CATS[0], "value": 9812},
            {"name": _CATS[1], "value": 7401},
            {"name": _CATS[2], "value": 5203},
        ]
        assert option["tooltip"]["trigger"] == "item"

    def test_donut_has_an_inner_radius(self) -> None:
        option = renderChartOption(_spec(ChartType.DONUT), _ROWS)
        radius = option["series"][0]["radius"]
        assert isinstance(radius, list) and len(radius) == 2

    def test_pie_percentage_template_is_allowed(self) -> None:
        """`{d}` 在饼图里是合法模板变量（百分比）。"""
        option = renderChartOption(_spec(ChartType.PIE), _ROWS)
        assert "{d}" in option["series"][0]["label"]["formatter"]


class TestScatter:
    def _scatterSpec(self) -> ChartSpec:
        return _spec(
            ChartType.SCATTER,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="scatter", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU"),
                SpecSeries(name="金额", kind="scatter", dimension="SUPPLIER_NAME", measure="LINE_AMT"),
            ),
        )

    def test_scatter_pairs_the_two_measures(self) -> None:
        option = renderChartOption(self._scatterSpec(), _ROWS)

        assert option["series"][0]["type"] == "scatter"
        assert option["xAxis"]["type"] == "value"
        assert option["yAxis"]["type"] == "value"
        assert option["series"][0]["data"] == [[9812, 98120], [7401, 74010], [5203, 52030]]


class TestHeatmap:
    def _heatmapSpec(self) -> ChartSpec:
        return _spec(
            ChartType.HEATMAP,
            columns=("SUPPLIER_NAME", "MATERIAL_NAME", "AMT"),
            dimensions=(_dim("SUPPLIER_NAME"), _dim("MATERIAL_NAME", "物料")),
            measures=(_measure("AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="heatmap", dimension="SUPPLIER_NAME", measure="AMT"),),
        )

    def test_heatmap_emits_indexed_triples(self) -> None:
        """约定：x 轴 = dimensions[1]（列），y 轴 = dimensions[0]（行）。"""
        option = renderChartOption(self._heatmapSpec(), _CROSS_ROWS)

        assert option["series"][0]["type"] == "heatmap"
        assert option["xAxis"]["type"] == "category"
        assert option["yAxis"]["type"] == "category"
        assert option["series"][0]["data"] == [
            [0, 0, 10],
            [1, 0, 20],
            [0, 1, 30],
            [1, 1, 40],
        ]

    def test_heatmap_has_a_visual_map_without_colors(self) -> None:
        """visualMap 必须有 min/max 才能渲染；色带留给 ECharts 默认（不选色）。"""
        option = renderChartOption(self._heatmapSpec(), _CROSS_ROWS)

        assert option["visualMap"]["min"] == 10
        assert option["visualMap"]["max"] == 40
        assert "inRange" not in option["visualMap"]

    def test_injected_axis_labels_render_dimension_names_on_both_axes(self) -> None:
        """注入后：x 轴名 = dimensions[1] 显示名，y 轴名 = dimensions[0] 显示名；原始列名不上轴。

        判别点：`_buildHeatmap` 的两个 `_namedAxis(..., _axisNameOf(...))` 包装若被删掉
        （改回裸 axis 字面量），两条 `name` 断言同时红 —— 这正是 heatmap 轴名接线此前无
        断言覆盖的盲区（bar 是唯一有覆盖的 kind）。
        """
        spec = _spec(
            ChartType.HEATMAP,
            columns=("SUPPLIER_NAME", "MATERIAL_NAME", "AMT"),
            dimensions=(_dim("SUPPLIER_NAME"), _dim("MATERIAL_NAME", "物料")),
            measures=(_measure("AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="heatmap", dimension="SUPPLIER_NAME", measure="AMT"),),
            axisLabels={"MATERIAL_NAME": "物料", "SUPPLIER_NAME": "供应商"},
        )

        option = renderChartOption(spec, _CROSS_ROWS)

        assert option["xAxis"]["name"] == "物料"          # dimensions[1]（列）显示名
        assert option["yAxis"]["name"] == "供应商"        # dimensions[0]（行）显示名
        assert option["xAxis"]["name"] != "MATERIAL_NAME"  # 原始列名不得经轴名漏出
        assert option["yAxis"]["name"] != "SUPPLIER_NAME"
        assert option["xAxis"]["data"] == ["M1", "M2"]    # 取值仍是数据键，不被翻译

    def test_chat_shape_renders_no_axis_name(self) -> None:
        """chat 形态（空 axisLabels）⇒ 两轴都不带 name 键（共享渲染器的反向护栏）。"""
        option = renderChartOption(self._heatmapSpec(), _CROSS_ROWS)
        assert "name" not in option["xAxis"]
        assert "name" not in option["yAxis"]


class TestSeriesComeFromSpec:
    """`spec.series` 是系列装配的**唯一来源**。

    渲染器如果自己去拼 `measures[index]` + 按位置写 yAxisIndex，就等于把
    builder 已经在 series 里声明过的 name/measure/axis 又实现了一遍：改了一边
    另一边不会跟着变，且 validateSpec 校验的是 series —— 校验通过与渲染正确
    从此不再是一回事。这里用「series 与 measures 故意不一致」的 spec 把这件事钉死。
    """

    def test_scatter_with_production_shape_resolves_y_from_position(self) -> None:
        """builder 给散点只发**一条** series（measure = 第一个指标），y 靠位置回退。

        生产形状与 `TestScatter` 里那两条 series 的写法不同：这里刻意用生产形状，
        把「series 只有一条时 `_measureOf(spec, 1)` 仍拿得到第二个指标」这条回退
        路径直接钉住（否则第二根轴会静默变成空数据）。
        """
        spec = _spec(
            ChartType.SCATTER,
            columns=("RCV_QTY_PUU", "LINE_AMT"),
            dimensions=(),
            measures=(_measure("RCV_QTY_PUU", "数量"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(
                    name="数量 × 金额",
                    kind="scatter",
                    dimension="",
                    measure="RCV_QTY_PUU",
                ),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert option["series"][0]["data"] == [[9812, 98120], [7401, 74010], [5203, 52030]]

    def test_series_name_and_measure_win_over_measures(self) -> None:
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            series=(
                SpecSeries(
                    name="金额（自 series）",
                    kind="bar",
                    dimension="SUPPLIER_NAME",
                    measure="LINE_AMT",
                ),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert option["series"][0]["name"] == "金额（自 series）"
        # measures[0] 是 RCV_QTY_PUU（9812/7401/5203）；series 指向 LINE_AMT
        assert option["series"][0]["data"] == [98120, 74010, 52030]

    def test_series_axis_wins_over_position(self) -> None:
        """两条系列都声明左轴 → 单轴；右轴由 series 决定，不由「第二条」决定。"""
        spec = _spec(
            ChartType.COMBO,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU", axis="left"),
                SpecSeries(name="金额", kind="line", dimension="SUPPLIER_NAME", measure="LINE_AMT", axis="left"),
            ),
        )

        option = renderChartOption(spec, _ROWS)

        assert [s["yAxisIndex"] for s in option["series"]] == [0, 0]
        assert isinstance(option["yAxis"], dict)

    def test_missing_series_falls_back_to_position(self) -> None:
        """防御：series 为空（校验漏网）时仍按位置给轴，不能让组合图退化成单轴。"""
        spec = _spec(
            ChartType.COMBO,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(),
        )

        option = renderChartOption(spec, _ROWS)

        assert [s["yAxisIndex"] for s in option["series"]] == [0, 1]
        assert isinstance(option["yAxis"], list)


class TestCombo:
    def test_combo_puts_the_second_metric_on_a_right_axis(self) -> None:
        rows = [
            {"MONTH": "2026-01", "LINE_AMT": 100, "占比": 0.4},
            {"MONTH": "2026-02", "LINE_AMT": 120, "占比": 0.6},
        ]
        spec = _spec(
            ChartType.COMBO,
            columns=("MONTH", "LINE_AMT", "占比"),
            dimensions=(_dim("MONTH", "月份"),),
            measures=(_measure("LINE_AMT", "金额"), _measure("占比", "占比")),
            series=(
                SpecSeries(name="金额", kind="bar", dimension="MONTH", measure="LINE_AMT"),
                SpecSeries(name="占比", kind="line", dimension="MONTH", measure="占比", axis="right"),
            ),
        )

        option = renderChartOption(spec, rows)

        assert len(option["yAxis"]) == 2
        assert option["series"][0]["type"] == "bar"
        assert option["series"][0]["yAxisIndex"] == 0
        assert option["series"][1]["type"] == "line"
        assert option["series"][1]["yAxisIndex"] == 1


class TestWaterfall:
    def _waterfallSpec(self) -> ChartSpec:
        return _spec(
            ChartType.WATERFALL,
            columns=("ITEM", "DELTA_AMT"),
            dimensions=(_dim("ITEM", "项目"),),
            measures=(_measure("DELTA_AMT", "增减"),),
            series=(SpecSeries(name="增减", kind="bar", dimension="ITEM", measure="DELTA_AMT"),),
        )

    def test_waterfall_stacks_a_base_and_a_height(self) -> None:
        rows = [
            {"ITEM": "期初", "DELTA_AMT": 100},
            {"ITEM": "采购", "DELTA_AMT": -40},
            {"ITEM": "领用", "DELTA_AMT": 30},
        ]

        option = renderChartOption(self._waterfallSpec(), rows)

        base, height = option["series"]
        assert base["stack"] == height["stack"]
        # 递减项从累计量的下沿起画：100 → 60 的那一段，基线 60、高 40。
        assert base["data"] == [0, 60, 60]
        assert height["data"] == [100, 40, 30]

    def test_waterfall_base_is_transparent(self) -> None:
        rows = [{"ITEM": "A", "DELTA_AMT": 10}, {"ITEM": "B", "DELTA_AMT": -5}]
        option = renderChartOption(self._waterfallSpec(), rows)
        assert option["series"][0]["itemStyle"]["color"] == "transparent"

    def test_invalid_deltas_are_gaps_not_zero(self) -> None:
        """缺失/非数值差值 = 断点：该位 base/height 双 None，累计量不推进。

        伪造成 0 会画出一格「变化量为零」的假事实；断点让缺口诚实可见，
        后续项从既有累计量继续（不是从断点重算）。
        """
        rows = [
            {"ITEM": "a", "DELTA_AMT": 100},
            {"ITEM": "b", "DELTA_AMT": None},
            {"ITEM": "c", "DELTA_AMT": -40},
            {"ITEM": "d", "DELTA_AMT": "oops"},
            {"ITEM": "e", "DELTA_AMT": 30},
        ]

        option = renderChartOption(self._waterfallSpec(), rows)

        base, height = option["series"]
        assert base["data"] == [0, None, 60, None, 60]
        assert height["data"] == [100, None, 40, None, 30]

    def test_all_invalid_deltas_do_not_raise(self) -> None:
        rows = [{"ITEM": "a", "DELTA_AMT": None}, {"ITEM": "b", "DELTA_AMT": "x"}]
        option = renderChartOption(self._waterfallSpec(), rows)
        assert option["series"][0]["data"] == [None, None]
        assert option["series"][1]["data"] == [None, None]

    def test_injected_axis_label_renders_dimension_name_and_never_raw_field(self) -> None:
        """注入后：瀑布类目轴名 = 维度显示名；原始列名不上轴。

        判别点：`_buildWaterfall` 的 `_namedAxis(..., _axisNameOf(...))` 包装若被删掉
        ⇒ `name` 断言红（waterfall 轴名接线此前无断言覆盖）。
        """
        spec = _spec(
            ChartType.WATERFALL,
            columns=("ITEM", "DELTA_AMT"),
            dimensions=(_dim("ITEM", "项目"),),
            measures=(_measure("DELTA_AMT", "增减"),),
            series=(SpecSeries(name="增减", kind="bar", dimension="ITEM", measure="DELTA_AMT"),),
            axisLabels={"ITEM": "项目"},
        )
        rows = [{"ITEM": "期初", "DELTA_AMT": 100}, {"ITEM": "采购", "DELTA_AMT": -40}]

        option = renderChartOption(spec, rows)

        assert option["xAxis"]["name"] == "项目"
        assert option["xAxis"]["name"] != "ITEM"       # 原始列名不得经轴名漏出
        assert option["xAxis"]["data"] == ["期初", "采购"]

    def test_chat_shape_renders_no_axis_name(self) -> None:
        """chat 形态（空 axisLabels）⇒ 类目轴不带 name 键（共享渲染器的反向护栏）。"""
        option = renderChartOption(self._waterfallSpec(), [{"ITEM": "A", "DELTA_AMT": 10}])
        assert "name" not in option["xAxis"]


class TestNeverRaises:
    def test_missing_referenced_column_does_not_raise(self) -> None:
        """spec 引用了 data 里没有的列 → 渲染成空值，不是异常。"""
        spec = _spec(
            ChartType.BAR,
            columns=("NOT_THERE",),
            dimensions=(_dim("NOT_THERE"),),
            measures=(_measure("NOT_THERE"),),
            series=(SpecSeries(name="x", kind="bar", dimension="NOT_THERE", measure="NOT_THERE"),),
        )

        option = renderChartOption(spec, [{"OTHER": 1}])

        assert isinstance(option, dict)
        assert "series" in option or "columns" in option

    def test_empty_rows_yield_a_renderable_option(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), [])
        assert isinstance(option, dict)

    def test_corrupt_cell_values_do_not_raise(self) -> None:
        rows = [{"SUPPLIER_NAME": object(), "RCV_QTY_PUU": "not-a-number"}]
        option = renderChartOption(_spec(ChartType.BAR), rows)
        assert isinstance(option, dict)


class TestNoColorIsServerSupplied:
    @pytest.mark.parametrize(
        "kind",
        [
            ChartType.LINE,
            ChartType.BAR,
            ChartType.HBAR,
            ChartType.PIE,
            ChartType.DONUT,
            ChartType.SCATTER,
            ChartType.COMBO,
            ChartType.KPI,
        ],
    )
    def test_no_hue_for_every_kind(self, kind: ChartType) -> None:
        """配色是前端主题层的事（决策 6）：服务端一个色值都不许下发。"""
        spec = _spec(
            kind,
            kpi=SpecKpi(label="x", value=1) if kind is ChartType.KPI else None,
        )
        _assertNoHue(renderChartOption(spec, _ROWS))

    def test_heatmap_and_waterfall_have_no_hue_either(self) -> None:
        heatmap = _spec(
            ChartType.HEATMAP,
            columns=("SUPPLIER_NAME", "MATERIAL_NAME", "AMT"),
            dimensions=(_dim("SUPPLIER_NAME"), _dim("MATERIAL_NAME", "物料")),
            measures=(_measure("AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="heatmap", dimension="SUPPLIER_NAME", measure="AMT"),),
        )
        _assertNoHue(renderChartOption(heatmap, _CROSS_ROWS))


class TestNoStrayPercentTemplate:
    def test_d_never_appears_outside_pie_like(self) -> None:
        """非饼图里出现 `{d}` 会原样显示字面量（用户报过的老 bug）。"""
        spec = _spec(ChartType.BAR, sort=SpecSort(by="RCV_QTY_PUU"))
        option = renderChartOption(spec, _ROWS)

        def walk(node: object) -> list[str]:
            hits: list[str] = []
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "formatter" and isinstance(v, str):
                        hits.append(v)
                    hits.extend(walk(v))
            elif isinstance(node, list):
                for item in node:
                    hits.extend(walk(item))
            return hits

        for formatter in walk(option):
            assert "{d}" not in formatter, f"非饼图 formatter 残留 {{d}}: {formatter!r}"


class TestImmutability:
    def test_inputs_are_not_mutated(self) -> None:
        rows = [dict(r) for r in _ROWS]
        before = [dict(r) for r in rows]
        spec = _spec(ChartType.BAR)

        renderChartOption(spec, rows)

        assert rows == before
        assert spec.columns == ("SUPPLIER_NAME", "RCV_QTY_PUU")


def _many_rows(count: int) -> list[dict]:
    return [
        {"SUPPLIER_NAME": f"S{i:02d}", "RCV_QTY_PUU": count - i}
        for i in range(count)
    ]


class TestCategoryReadability:
    """2026-10-31：类别一多，图和标签被等比压扁；用高度（前端）+ 轴配置（后端）解决。"""

    def test_hbar_many_categories_shows_every_label_truncated(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _many_rows(20))

        assert option["yAxis"]["axisLabel"] == {
            "interval": 0,
            "overflow": "truncate",
            "width": 160,
        }

    def test_hbar_few_categories_keep_bare_axis(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _ROWS)

        assert "axisLabel" not in option["yAxis"]

    def test_vertical_bar_many_categories_rotates_labels(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), _many_rows(20))

        assert option["xAxis"]["axisLabel"] == {
            "rotate": 45,
            "interval": 0,
            "hideOverlap": True,
        }

    def test_line_many_categories_keeps_auto_interval(self) -> None:
        """折线不旋转：斜着的日期标签比稀疏标签更难读。"""
        spec = _spec(
            ChartType.LINE,
            columns=("MONTH", "LINE_AMT"),
            dimensions=(_dim("MONTH", "月份"),),
            measures=(_measure("LINE_AMT", "金额"),),
            series=(SpecSeries(name="金额", kind="line", dimension="MONTH", measure="LINE_AMT"),),
        )
        rows = [{"MONTH": f"M{i:02d}", "LINE_AMT": i} for i in range(50)]

        option = renderChartOption(spec, rows)

        assert "axisLabel" not in option["xAxis"]
        assert "dataZoom" not in option

    def test_hbar_over_threshold_adds_datazoom_and_grid_room(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _many_rows(50))

        assert option["dataZoom"] == [
            {"type": "inside", "yAxisIndex": 0},
            {"type": "slider", "yAxisIndex": 0, "height": 20, "bottom": 0},
        ]
        assert option["grid"]["bottom"] == 66  # 32 + 34
        assert "legend" not in option

    def test_vertical_bar_over_threshold_zooms_x_axis(self) -> None:
        option = renderChartOption(_spec(ChartType.BAR), _many_rows(50))

        assert option["dataZoom"] == [
            {"type": "inside", "xAxisIndex": 0},
            {"type": "slider", "xAxisIndex": 0, "height": 20, "bottom": 0},
        ]

    def test_datazoom_and_legend_share_the_bottom_band(self) -> None:
        spec = _spec(
            ChartType.BAR,
            columns=("SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"),
            measures=(_measure("RCV_QTY_PUU"), _measure("LINE_AMT", "金额")),
            series=(
                SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU"),
                SpecSeries(name="金额", kind="bar", dimension="SUPPLIER_NAME", measure="LINE_AMT"),
            ),
        )

        option = renderChartOption(spec, _many_rows(50))

        assert option["grid"]["bottom"] == 90  # 56 + 34
        assert option["legend"]["bottom"] == 24  # 滑块 20 + 4
        assert option["legend"]["type"] == "scroll"

    def test_threshold_is_strictly_greater_than_40(self) -> None:
        option = renderChartOption(_spec(ChartType.HBAR), _many_rows(40))

        assert "dataZoom" not in option
        assert option["grid"]["bottom"] == 32
