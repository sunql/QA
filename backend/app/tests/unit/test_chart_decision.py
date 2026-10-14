"""图表决策引擎：规则表 0-14 + 候选集 + 消歧 + ETL 列黑名单。

设计要点（plan `piped-beaming-possum.md`）：
- **规则优先**：有序规则表，首个命中者胜，顺序即优先级。
- **LLM 只在规则歧义时介入**，且只给一个**语义标签**（TREND/SHARE/RANK/COMPARE/
  RELATION/DETAIL/KPI），由 `resolvedByLabel` 把标签映射回候选集里的某个 kind；
  标签不在白名单、或映射出的 kind 不满足形状守卫 → 退回规则原判。
- **ETL 列必须拉黑**：同一张表既有业务日期也有 ETL 加载时间，
  不拉黑就会出现「按 ETL_LOAD_TS 画折线」。
- **无信息 → 表格**（规则的终点也是降级终点）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.enums import ChartType
from app.domain.query_plan import Aggregation, QueryPlan, SortSpec
from app.services.chart_decision import (
    ChartDecision,
    ChartSignals,
    buildChartSignals,
    decideChartKind,
    resolveByLabel,
)
from app.services.chart_thresholds import ChartThresholds

_THRESHOLDS = ChartThresholds(
    pieMaxRows=6, hbarMinRows=15, heatmapMinCoverage=0.6, topNMax=20
)

_SUPPLIER_ROWS = [
    {"SUPPLIER_NAME": "B125 浙江力航", "RCV_QTY_PUU": 9812},
    {"SUPPLIER_NAME": "B019 温州圣特", "RCV_QTY_PUU": 7401},
    {"SUPPLIER_NAME": "B153 天津精一", "RCV_QTY_PUU": 5203},
]

_DATE_ROWS = [
    {"RECEIPT_DATE": "2026-05-01", "LINE_AMT": 100},
    {"RECEIPT_DATE": "2026-05-02", "LINE_AMT": 200},
]


def _plan(**overrides) -> QueryPlan:
    base: dict = {"target": "DWD_GOODS_RECEIPT_DTL"}
    base.update(overrides)
    return QueryPlan(**base)


def _signals(
    columns: list[str],
    data: list[dict],
    *,
    plan: QueryPlan | None = None,
    question: str = "",
) -> ChartSignals:
    return buildChartSignals(plan if plan is not None else _plan(), columns, data, question)


class TestEmptyAndDegenerate:
    def test_no_columns_is_table(self) -> None:
        decision = decideChartKind(_signals([], []), _THRESHOLDS)
        assert decision.kind is ChartType.TABLE
        assert decision.ruleId == "R00_EMPTY_TABLE"

    def test_no_rows_is_table(self) -> None:
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], []), _THRESHOLDS
        )
        assert decision.kind is ChartType.TABLE

    def test_decision_is_frozen(self) -> None:
        decision = decideChartKind(_signals([], []), _THRESHOLDS)
        with pytest.raises(FrozenInstanceError):
            decision.kind = ChartType.BAR  # type: ignore[misc]


class TestSingleValueKpi:
    def test_one_row_one_measure_is_kpi(self) -> None:
        decision = decideChartKind(
            _signals(
                ["RCV_QTY_PUU"],
                [{"RCV_QTY_PUU": 22416}],
                plan=_plan(aggregations=(Aggregation(function="SUM", property="RCV_QTY_PUU"),)),
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.KPI
        assert decision.ruleId == "R01_SINGLE_VALUE_KPI"

    def test_one_row_with_two_measures_is_not_kpi(self) -> None:
        """两个数值同现一行是「两个指标并列」，不是单值卡（落 R01S 降级成表格）。"""
        signals = _signals(
            ["LINE_AMT", "RCV_QTY_PUU"], [{"LINE_AMT": 1, "RCV_QTY_PUU": 2}]
        )
        decision = decideChartKind(signals, _THRESHOLDS)
        assert decision.kind is not ChartType.KPI
        assert decision.kind is ChartType.TABLE

    def test_many_rows_is_not_kpi(self) -> None:
        signals = _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], _SUPPLIER_ROWS)
        assert decideChartKind(signals, _THRESHOLDS).kind is not ChartType.KPI


class TestSingleRowDegenerate:
    """单行多指标 / 多维不是图：散点只有一个点、热力图只有一个格子。

    R01 只认「单行单指标」是 KPI，其余单行形状会一路掉到 R08（散点）/R09（热力图），
    渲染出来是一张只有一个点的散布图或只有一个格子的热力图 —— 读不出任何东西。
    这些形状统一由 R01S 落成表格（表格至少能把两个数并排显示）。
    """

    def test_one_row_two_measures_without_dimension_is_table(self) -> None:
        """`SELECT SUM(amount), SUM(qty)` 不带 GROUP BY —— NL2SQL 的真实产物。"""
        decision = decideChartKind(
            _signals(["LINE_AMT", "RCV_QTY_PUU"], [{"LINE_AMT": 36096, "RCV_QTY_PUU": 540}]),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.TABLE
        assert decision.ruleId == "R01S_SINGLE_ROW_TABLE"

    def test_one_row_two_measures_with_dimension_is_table(self) -> None:
        decision = decideChartKind(
            _signals(
                ["SUPPLIER_NAME", "LINE_AMT", "RCV_QTY_PUU"],
                [{"SUPPLIER_NAME": "B125", "LINE_AMT": 36096, "RCV_QTY_PUU": 540}],
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.TABLE

    def test_one_row_two_dimensions_is_table(self) -> None:
        """两维一指标的单行：热力图只有一个格子（R09 的完备度算出来是 1.0）。"""
        decision = decideChartKind(
            _signals(
                ["SUPPLIER_NAME", "MATERIAL_NO", "RCV_QTY_PUU"],
                [{"SUPPLIER_NAME": "B125", "MATERIAL_NO": "M1", "RCV_QTY_PUU": 9}],
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.TABLE
        assert decision.ruleId == "R01S_SINGLE_ROW_TABLE"

    def test_one_row_one_dimension_two_measures_is_table(self) -> None:
        signals = _signals(
            ["SUPPLIER_NAME", "LINE_AMT", "RCV_QTY_PUU"],
            [{"SUPPLIER_NAME": "B125", "LINE_AMT": 1, "RCV_QTY_PUU": 2}],
        )
        assert decideChartKind(signals, _THRESHOLDS).kind is ChartType.TABLE

    def test_one_row_single_measure_still_kpi(self) -> None:
        """守卫不能误伤 R01：单行单指标仍是指标卡。"""
        decision = decideChartKind(
            _signals(
                ["RCV_QTY_PUU"],
                [{"RCV_QTY_PUU": 22416}],
                plan=_plan(aggregations=(Aggregation(function="SUM", property="RCV_QTY_PUU"),)),
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.KPI

    def test_two_rows_two_measures_is_still_scatter(self) -> None:
        """守卫只拦单行：两行两指标仍是散点（有真实的相关性可看）。"""
        decision = decideChartKind(
            _signals(
                ["LINE_AMT", "RCV_QTY_PUU"],
                [{"LINE_AMT": 1, "RCV_QTY_PUU": 2}, {"LINE_AMT": 3, "RCV_QTY_PUU": 4}],
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.SCATTER


class TestShareByFormula:
    """占比的确定性信号是 ``Aggregation.formula``（validatePlan 已强制别名含占比时必填）。"""

    def _sharePlan(self) -> QueryPlan:
        return _plan(
            aggregations=(
                Aggregation(
                    function="SUM",
                    property="RCV_QTY_PUU",
                    alias="占比",
                    formula="SUM(RCV_QTY_PUU) / SUM(SUM(RCV_QTY_PUU)) OVER ()",
                ),
            ),
            groupBy=("SUPPLIER_NAME",),
        )

    def test_share_within_pie_limit_is_donut(self) -> None:
        rows = [
            {"SUPPLIER_NAME": "B125", "占比": 0.5},
            {"SUPPLIER_NAME": "B019", "占比": 0.3},
            {"SUPPLIER_NAME": "B153", "占比": 0.2},
        ]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "占比"], rows, plan=self._sharePlan()),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.DONUT
        assert decision.ruleId == "R02_SHARE_DONUT"
        assert decision.ambiguous is False

    def test_share_over_pie_limit_is_hbar(self) -> None:
        rows = [{"SUPPLIER_NAME": f"B{i:03d}", "占比": i / 100} for i in range(9)]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "占比"], rows, plan=self._sharePlan()),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.HBAR
        assert decision.ruleId == "R03_SHARE_OVERFLOW_HBAR"

    def test_share_at_exactly_the_limit_is_donut(self) -> None:
        """边界：行数 == pieMaxRows 仍画环形（>才是溢出）。"""
        rows = [
            {"SUPPLIER_NAME": f"B{i}", "占比": i / 100}
            for i in range(_THRESHOLDS.pieMaxRows)
        ]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "占比"], rows, plan=self._sharePlan()),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.DONUT


class TestTopN:
    def test_row_limit_with_desc_sort_is_hbar(self) -> None:
        decision = decideChartKind(
            _signals(
                ["SUPPLIER_NAME", "RCV_QTY_PUU"],
                _SUPPLIER_ROWS,
                plan=_plan(
                    groupBy=("SUPPLIER_NAME",),
                    sortBy=(SortSpec(property="RCV_QTY_PUU", direction="desc"),),
                    rowLimit=3,
                ),
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.HBAR
        assert decision.ruleId == "R04_TOPN_HBAR"

    def test_per_group_limit_is_hbar(self) -> None:
        """「每个供应商各自供货量前三的物料」：2 维（分区维 + 组内维）但仍是排名。"""
        rows = [
            {"SUPPLIER_NAME": "B125", "MATERIAL_NAME": "M1", "RCV_QTY_PUU": 900},
            {"SUPPLIER_NAME": "B125", "MATERIAL_NAME": "M2", "RCV_QTY_PUU": 800},
            {"SUPPLIER_NAME": "B019", "MATERIAL_NAME": "M3", "RCV_QTY_PUU": 700},
            {"SUPPLIER_NAME": "B019", "MATERIAL_NAME": "M4", "RCV_QTY_PUU": 600},
            {"SUPPLIER_NAME": "B153", "MATERIAL_NAME": "M5", "RCV_QTY_PUU": 500},
            {"SUPPLIER_NAME": "B153", "MATERIAL_NAME": "M6", "RCV_QTY_PUU": 400},
        ]
        decision = decideChartKind(
            _signals(
                ["SUPPLIER_NAME", "MATERIAL_NAME", "RCV_QTY_PUU"],
                rows,
                plan=_plan(
                    groupBy=("SUPPLIER_NAME", "MATERIAL_NAME"),
                    partitionBy=("SUPPLIER_NAME",),
                    perGroupLimit=3,
                ),
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.HBAR
        assert decision.ruleId == "R04_TOPN_HBAR"

    def test_row_limit_without_desc_sort_is_not_topn(self) -> None:
        """裸 LIMIT 是截断不是排名：不知道顺序就不该按 Top N 画。"""
        signals = _signals(
            ["SUPPLIER_NAME", "RCV_QTY_PUU"],
            _SUPPLIER_ROWS,
            plan=_plan(groupBy=("SUPPLIER_NAME",), rowLimit=3),
        )
        assert decideChartKind(signals, _THRESHOLDS).ruleId != "R04_TOPN_HBAR"

    def test_ranking_question_over_time_dim_still_ranks(self) -> None:
        """「供货量最多的 3 个月」是排名，不是趋势 —— 问句意图压过时间维。"""
        rows = [{"MONTH": f"2026-{m:02d}", "RCV_QTY_PUU": m * 100} for m in range(1, 13)]
        decision = decideChartKind(
            _signals(
                ["MONTH", "RCV_QTY_PUU"], rows, question="供货量最多的3个月是哪些"
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.HBAR


class TestTimeTrend:
    def test_date_column_with_measure_is_line(self) -> None:
        rows = [
            {"RECEIPT_DATE": "2026-05-01", "LINE_AMT": 100},
            {"RECEIPT_DATE": "2026-05-02", "LINE_AMT": 200},
        ]
        decision = decideChartKind(
            _signals(["RECEIPT_DATE", "LINE_AMT"], rows), _THRESHOLDS
        )
        assert decision.kind is ChartType.LINE
        assert decision.ruleId == "R07_TREND_LINE"

    def test_trend_cue_in_question_removes_the_ambiguity(self) -> None:
        """问句自己说了「趋势」= 确定性的用户意图，不必再让分类器猜一遍。"""
        decision = decideChartKind(
            _signals(["RECEIPT_DATE", "LINE_AMT"], _DATE_ROWS, question="按月的入库金额趋势"),
            _THRESHOLDS,
        )

        assert decision.kind is ChartType.LINE
        assert decision.ambiguous is False

    def test_silent_trend_question_stays_ambiguous(self) -> None:
        """没线索时仍算歧义（趋势 vs 分类比较）—— 收窄 LLM 面只在有线索时发生。"""
        decision = decideChartKind(
            _signals(["RECEIPT_DATE", "LINE_AMT"], _DATE_ROWS), _THRESHOLDS
        )

        assert decision.ruleId == "R07_TREND_LINE"
        assert decision.ambiguous is True

    def test_etl_column_is_never_a_trend_axis(self) -> None:
        """完整时间形状，但唯一「时间列」是 ETL 加载时间 → 绝不画折线。"""
        rows = [
            {"ETL_LOAD_TS": "2026-05-01", "LINE_AMT": 100},
            {"ETL_LOAD_TS": "2026-05-02", "LINE_AMT": 200},
        ]
        signals = _signals(["ETL_LOAD_TS", "LINE_AMT"], rows, plan=_plan(groupBy=("ETL_LOAD_TS",)))

        assert signals.timeColumns == ()
        assert "ETL_LOAD_TS" in signals.etlColumns
        assert decideChartKind(signals, _THRESHOLDS).kind is not ChartType.LINE

    @pytest.mark.parametrize(
        "etlColumn", ["ETL_LOAD_TS", "UPDATE_DATE", "CREATE_TIME", "UPDATE_TIME"]
    )
    def test_each_etl_column_family_is_denied(self, etlColumn: str) -> None:
        rows = [{etlColumn: "2026-05-01", "LINE_AMT": 100}]
        signals = _signals([etlColumn, "LINE_AMT"], rows, plan=_plan(groupBy=(etlColumn,)))
        assert signals.timeColumns == ()

    def test_period_strings_in_groupby_are_a_time_dim(self) -> None:
        """`202605`/`5月` 这类期间值本身是 STRING，但被 plan 当维度分组时就是时间维。

        这是列类型推断的已知盲区（TIME 只认 `\\d{4}-\\d{2}-\\d{2}`），不补这条
        「按月趋势」永远出不了折线。
        """
        rows = [{"MONTH": "202605", "LINE_AMT": 100}, {"MONTH": "202606", "LINE_AMT": 200}]
        signals = _signals(["MONTH", "LINE_AMT"], rows, plan=_plan(groupBy=("MONTH",)))

        assert signals.timeColumns == ("MONTH",)
        assert decideChartKind(signals, _THRESHOLDS).kind is ChartType.LINE

    def test_period_strings_outside_groupby_are_not_time(self) -> None:
        """没被 plan 当维度用的 4 位字符串可能是编码，不能猜成时间。"""
        rows = [{"MATERIAL_CODE": "1001", "LINE_AMT": 100}, {"MATERIAL_CODE": "1002", "LINE_AMT": 200}]
        signals = _signals(["MATERIAL_CODE", "LINE_AMT"], rows)
        assert signals.timeColumns == ()


class TestRelationScatter:
    def test_two_measures_one_dimension_is_scatter(self) -> None:
        rows = [
            {"SUPPLIER_NAME": f"B{i}", "RCV_QTY_PUU": i, "LINE_AMT": i * 10} for i in range(8)
        ]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"], rows), _THRESHOLDS
        )
        assert decision.kind is ChartType.SCATTER
        assert decision.ruleId == "R08_RELATION_SCATTER"


class TestMultiDimension:
    """两维一指标的交叉表 —— 真实的交叉表来自 `GROUP BY dim1, dim2`。"""

    _CROSS_TAB_COLUMNS = ["SUPPLIER_NAME", "MATERIAL_NAME", "AMT"]
    _CROSS_TAB_PLAN = _plan(
        groupBy=("SUPPLIER_NAME", "MATERIAL_NAME"),
        aggregations=(Aggregation(function="SUM", property="AMT"),),
    )

    def _denseRows(self) -> list[dict]:
        """3×4 全交叉（完备度 1.0）。"""
        return [
            {"SUPPLIER_NAME": f"S{s}", "MATERIAL_NAME": f"M{m}", "AMT": 1}
            for s in range(3)
            for m in range(4)
        ]

    def test_dense_two_dimensions_is_heatmap(self) -> None:
        decision = decideChartKind(
            _signals(
                self._CROSS_TAB_COLUMNS, self._denseRows(), plan=self._CROSS_TAB_PLAN
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.HEATMAP
        assert decision.ruleId == "R09_MULTIDIM_HEATMAP"

    def test_sparse_two_dimensions_falls_back_to_bar(self) -> None:
        """稀疏交叉表画成热力图是大片空白 —— 退普通柱状。

        对角稀疏：20 行 / (20×20) = 0.05，远低于 0.6 门槛。
        """
        sparse = [
            {"SUPPLIER_NAME": f"S{s}", "MATERIAL_NAME": f"M{s}", "AMT": 1}
            for s in range(20)
        ]
        decision = decideChartKind(
            _signals(self._CROSS_TAB_COLUMNS, sparse, plan=self._CROSS_TAB_PLAN),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.BAR
        assert decision.ruleId == "R10_MULTIDIM_BAR"

    def test_coverage_is_reported_on_signals(self) -> None:
        signals = _signals(
            self._CROSS_TAB_COLUMNS, self._denseRows(), plan=self._CROSS_TAB_PLAN
        )
        assert signals.heatmapCoverage == pytest.approx(1.0)

    def test_ungrouped_two_dimensions_is_a_detail_table_not_a_heatmap(self) -> None:
        """没有聚合也没有分组的「两维一指标」是行清单，画热力图等于把明细当统计量。"""
        rows = [
            {"PO_NO": "P001", "SUPPLIER_NAME": "B125", "LINE_AMT": 100},
            {"PO_NO": "P002", "SUPPLIER_NAME": "B019", "LINE_AMT": 200},
        ]
        decision = decideChartKind(
            _signals(["PO_NO", "SUPPLIER_NAME", "LINE_AMT"], rows, plan=_plan()),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.TABLE
        assert decision.ruleId == "R13_RAW_DETAIL_TABLE"


class TestCategoryBarAndHbar:
    def test_one_dimension_one_measure_is_bar(self) -> None:
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], _SUPPLIER_ROWS), _THRESHOLDS
        )
        assert decision.kind is ChartType.BAR
        assert decision.ruleId == "R12_CATEGORY_BAR"

    def test_many_categories_flip_to_hbar(self) -> None:
        rows = [{"SUPPLIER_NAME": f"B{i:03d}", "RCV_QTY_PUU": i} for i in range(40)]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], rows), _THRESHOLDS
        )
        assert decision.kind is ChartType.HBAR
        assert decision.ruleId == "R11_HBAR_MANY_ROWS"

    def test_hbar_threshold_boundary_is_exclusive(self) -> None:
        """行数 == hbarMinRows 仍画竖柱（>才是横过来）。"""
        rows = [
            {"SUPPLIER_NAME": f"B{i:03d}", "RCV_QTY_PUU": i}
            for i in range(_THRESHOLDS.hbarMinRows)
        ]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], rows), _THRESHOLDS
        )
        assert decision.kind is ChartType.BAR


class TestComboAndWaterfall:
    def test_time_dim_with_two_measures_is_combo(self) -> None:
        rows = [
            {"MONTH": "2026-01", "LINE_AMT": 100, "RATE": 0.1},
            {"MONTH": "2026-02", "LINE_AMT": 120, "RATE": 0.2},
        ]
        decision = decideChartKind(
            _signals(
                ["MONTH", "LINE_AMT", "RATE"],
                rows,
                plan=_plan(groupBy=("MONTH",)),
                question="按月对比金额和占比",
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.COMBO
        assert decision.ruleId == "R06_COMBO"

    def test_time_dim_with_measure_and_formula_is_combo(self) -> None:
        """量 + 占比同轴也是柱线组合的形状（占比有 formula 时无需问句关键词）。"""
        rows = [
            {"MONTH": "2026-01", "LINE_AMT": 100, "占比": 0.4},
            {"MONTH": "2026-02", "LINE_AMT": 120, "占比": 0.6},
        ]
        plan = _plan(
            aggregations=(
                Aggregation(function="SUM", property="LINE_AMT"),
                Aggregation(
                    function="SUM", property="LINE_AMT", alias="占比", formula="x"
                ),
            ),
            groupBy=("MONTH",),
        )
        decision = decideChartKind(
            _signals(["MONTH", "LINE_AMT", "占比"], rows, plan=plan), _THRESHOLDS
        )
        assert decision.kind is ChartType.COMBO

    def test_waterfall_question_with_signed_values(self) -> None:
        rows = [
            {"ITEM": "期初", "DELTA_AMT": 100},
            {"ITEM": "采购", "DELTA_AMT": -40},
            {"ITEM": "领用", "DELTA_AMT": 30},
        ]
        decision = decideChartKind(
            _signals(["ITEM", "DELTA_AMT"], rows, question="这个金额的构成拆解一下"),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.WATERFALL
        assert decision.ruleId == "R05_WATERFALL"

    def test_waterfall_question_without_signed_values_degrades_to_bar(self) -> None:
        """没有增减量/负值就没有可拆解的构成 —— 画瀑布是编故事。"""
        decision = decideChartKind(
            _signals(
                ["SUPPLIER_NAME", "RCV_QTY_PUU"], _SUPPLIER_ROWS, question="构成拆解一下"
            ),
            _THRESHOLDS,
        )
        assert decision.kind is ChartType.BAR


class TestAmbiguityAndLabelResolution:
    def _ambiguousSignals(self) -> ChartSignals:
        """1 维 + 1 指标，无 formula、无排序 —— 占比与分类比较形状相同。"""
        return _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], _SUPPLIER_ROWS)

    def test_candidate_set_has_more_than_one_kind(self) -> None:
        decision = decideChartKind(self._ambiguousSignals(), _THRESHOLDS)
        assert decision.ambiguous is True
        assert len(set(decision.candidates)) > 1

    def test_label_share_picks_donut(self) -> None:
        decision = decideChartKind(self._ambiguousSignals(), _THRESHOLDS)
        resolved = resolveByLabel(decision, self._ambiguousSignals(), "SHARE", _THRESHOLDS)
        assert resolved.kind is ChartType.DONUT
        assert resolved.labelHint == "SHARE"

    def test_label_rank_picks_hbar(self) -> None:
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        assert resolveByLabel(decision, signals, "RANK", _THRESHOLDS).kind is ChartType.HBAR

    def test_label_compare_keeps_bar(self) -> None:
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        assert resolveByLabel(decision, signals, "COMPARE", _THRESHOLDS).kind is ChartType.BAR

    def test_label_outside_whitelist_is_ignored(self) -> None:
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        resolved = resolveByLabel(decision, signals, "ECharts", _THRESHOLDS)
        assert resolved.kind is decision.kind
        assert resolved.labelHint is None

    def test_none_label_is_ignored(self) -> None:
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        assert resolveByLabel(decision, signals, None, _THRESHOLDS).kind is decision.kind

    def test_label_requiring_impossible_shape_falls_back(self) -> None:
        """RELATION 要两个指标，这里只有一个 → 形状守卫拦下，退回规则原判。"""
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        assert resolveByLabel(decision, signals, "RELATION", _THRESHOLDS).kind is ChartType.BAR

    def test_unambiguous_decision_needs_no_label(self) -> None:
        """确定性规则（formula → 环形）下 candidates 只有一族，不该调 LLM。"""
        plan = _plan(
            aggregations=(
                Aggregation(function="SUM", property="RCV_QTY_PUU", alias="占比", formula="x"),
            ),
            groupBy=("SUPPLIER_NAME",),
        )
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "占比"], _SUPPLIER_ROWS, plan=plan), _THRESHOLDS
        )
        assert decision.ambiguous is False

    def test_labelled_decision_is_frozen_and_carries_hint(self) -> None:
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        resolved = resolveByLabel(decision, signals, "SHARE", _THRESHOLDS)
        assert isinstance(resolved, ChartDecision)
        assert resolved.ruleId == decision.ruleId

    def test_trend_label_without_time_dim_cannot_pick_line(self) -> None:
        """没有时间维就没有趋势可言 —— TREND 也不能把类别轴画成折线。"""
        signals = self._ambiguousSignals()
        decision = decideChartKind(signals, _THRESHOLDS)
        assert resolveByLabel(decision, signals, "TREND", _THRESHOLDS).kind is ChartType.BAR


class TestQuestionCuesResolveWithoutTheClassifier:
    """问句里的确定性线索（「占比」「趋势」）直接定音，省掉一次分类往返。

    这不是「多加一条启发式」，而是**把降级路径上的 LLM 调用点收窄**：之前
    「用户明说了占比」仍要走分类器，而分类器能给的最优答案就是 SHARE→DONUT ——
    确定性线索给出同一个答案，却不引入一次会失败/会判错的往返。
    """

    def test_share_question_picks_donut_without_ambiguity(self) -> None:
        decision = decideChartKind(
            _signals(
                ["SUPPLIER_NAME", "RCV_QTY_PUU"],
                _SUPPLIER_ROWS,
                question="各供应商的入库量占比是多少",
            ),
            _THRESHOLDS,
        )

        assert decision.kind is ChartType.DONUT
        assert decision.ambiguous is False
        assert decision.ruleId == "R12S_QUESTION_SHARE_DONUT"

    def test_share_question_over_pie_limit_falls_back_to_hbar(self) -> None:
        """「占比」线索也越不过形状：9 行放不进环形，横过来。"""
        rows = [{"SUPPLIER_NAME": f"B{i:03d}", "RCV_QTY_PUU": i} for i in range(9)]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "RCV_QTY_PUU"], rows, question="各供应商占比"),
            _THRESHOLDS,
        )

        assert decision.kind is ChartType.HBAR
        assert decision.ambiguous is False

    def test_share_cue_yields_to_an_explicit_formula(self) -> None:
        """有 formula 时走 R02（规则 2 在 R12S 之前），线索不改变已有结论。"""
        plan = _plan(
            aggregations=(
                Aggregation(function="SUM", property="RCV_QTY_PUU", alias="占比", formula="x"),
            ),
            groupBy=("SUPPLIER_NAME",),
        )
        rows = [
            {"SUPPLIER_NAME": "B125", "占比": 0.5},
            {"SUPPLIER_NAME": "B019", "占比": 0.3},
            {"SUPPLIER_NAME": "B153", "占比": 0.2},
        ]
        decision = decideChartKind(
            _signals(["SUPPLIER_NAME", "占比"], rows, plan=plan, question="各供应商占比"),
            _THRESHOLDS,
        )

        assert decision.ruleId == "R02_SHARE_DONUT"

    def test_share_cue_without_a_usable_dimension_is_ignored(self) -> None:
        """两个指标没有维度 → 形状不支持环形，线索被形状守卫挡下（走分类器）。"""
        rows = [{"IN_AMT": 1, "OUT_AMT": 2}, {"IN_AMT": 3, "OUT_AMT": 4}]
        decision = decideChartKind(
            _signals(["IN_AMT", "OUT_AMT"], rows, question="各供应商占比"), _THRESHOLDS
        )

        assert decision.kind is not ChartType.DONUT

    def test_no_unreachable_question_cue(self) -> None:
        """线索只能挂在「线索条件成立时仍会被这条规则命中」的规则上。

        反例（曾经有过）：R12_CATEGORY_BAR 上挂 `questionShare` 线索 —— 占比问句在
        R12S 就被接住，走到 R12 时它必为 False，那条线索一次都不会触发。
        """
        from app.services.chart_decision import _QUESTION_CUES

        assert set(_QUESTION_CUES) == {"R07_TREND_LINE"}
