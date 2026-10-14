"""ChartSpec 标准化图表规格：模型 + 校验 + 降级。

设计要点（见 plan `piped-beaming-possum.md`）：
- spec 是**内部领域对象**（不进线上契约），故用 frozen dataclass，与 QueryPlan 同范式。
- 校验只做一件事：**引用的列必须真实存在**。spec 由代码从 QueryPlan/列名派生，
  不是 LLM 产出，所以没有「kind 非法」这类解析期风险，唯一能把图渲染坏的是
  「引用了一列并不在结果里」。
- **校验失败一律降级为 TABLE，绝不产出空图** —— 前端的渲染门是
  `chartType && chartOption`（MessageItem.tsx:133），发一个渲染不出来的 kind
  等于让用户什么都看不到。
"""

from __future__ import annotations

import pytest

from app.domain.chart_spec import (
    SPEC_VERSION,
    ChartSpec,
    SpecDimension,
    SpecKpi,
    SpecMeasure,
    SpecSeries,
    SpecSort,
    coerceSpec,
    tableSpec,
    validateSpec,
)
from app.domain.enums import ChartType

_COLUMNS = ["SUPPLIER_NAME", "RCV_QTY_PUU", "LINE_AMT"]


def _dim(field: str = "SUPPLIER_NAME") -> SpecDimension:
    return SpecDimension(field=field, label="供应商")


def _measure(field: str = "RCV_QTY_PUU") -> SpecMeasure:
    return SpecMeasure(field=field, label="供货量", agg="SUM")


def _series() -> SpecSeries:
    return SpecSeries(name="供货量", kind="bar", dimension="SUPPLIER_NAME", measure="RCV_QTY_PUU")


def _barSpec(**overrides) -> ChartSpec:
    base = {
        "kind": ChartType.BAR,
        "title": "供货量 按 供应商",
        "dimensions": (_dim(),),
        "measures": (_measure(),),
        "series": (_series(),),
    }
    base.update(overrides)
    return ChartSpec(**base)


class TestValidateAcceptsWellFormedSpecs:
    def test_bar_spec_with_existing_columns_is_valid(self) -> None:
        assert validateSpec(_barSpec(), _COLUMNS) is None

    def test_spec_version_is_carried(self) -> None:
        assert _barSpec().specVersion == SPEC_VERSION

    def test_table_spec_needs_no_structural_shape(self) -> None:
        """TABLE 是降级终点，本身不受「必须有 series」约束。"""
        assert validateSpec(tableSpec(_COLUMNS), _COLUMNS) is None


class TestValidateRejectsBrokenReferences:
    def test_unknown_dimension_field_is_rejected(self) -> None:
        spec = _barSpec(dimensions=(_dim("NOT_A_COLUMN"),))
        reason = validateSpec(spec, _COLUMNS)
        assert reason is not None
        assert "NOT_A_COLUMN" in reason

    def test_unknown_measure_field_is_rejected(self) -> None:
        spec = _barSpec(measures=(_measure("NOT_A_COLUMN"),))
        assert validateSpec(spec, _COLUMNS) is not None

    def test_unknown_sort_field_is_rejected(self) -> None:
        spec = _barSpec(sort=SpecSort(by="NOT_A_COLUMN"))
        assert validateSpec(spec, _COLUMNS) is not None

    def test_unknown_series_field_is_rejected(self) -> None:
        spec = _barSpec(
            series=(SpecSeries(name="x", kind="bar", dimension="NOT_A_COLUMN", measure="RCV_QTY_PUU"),)
        )
        assert validateSpec(spec, _COLUMNS) is not None

    def test_empty_series_dimension_is_not_a_bad_reference(self) -> None:
        """散点图可以没有维度（x/y 都是指标），空串是「不引用维度」而非坏引用。"""
        spec = _barSpec(
            kind=ChartType.SCATTER,
            dimensions=(),
            measures=(_measure(), _measure("LINE_AMT")),
            series=(SpecSeries(name="x", kind="scatter", dimension="", measure="RCV_QTY_PUU"),),
        )
        assert validateSpec(spec, _COLUMNS) is None


class TestValidateRejectsWrongShapePerKind:
    def test_chart_kind_without_series_is_rejected(self) -> None:
        spec = _barSpec(series=())
        assert validateSpec(spec, _COLUMNS) is not None

    def test_chart_kind_without_measures_is_rejected(self) -> None:
        spec = _barSpec(measures=(), series=())
        assert validateSpec(spec, _COLUMNS) is not None

    def test_more_than_two_dimensions_is_rejected(self) -> None:
        spec = _barSpec(
            dimensions=(_dim(), SpecDimension(field="LINE_AMT", label="金额"), SpecDimension(field="SUPPLIER_NAME", label="供应商2")),
        )
        assert validateSpec(spec, _COLUMNS) is not None

    def test_heatmap_requires_exactly_two_dimensions(self) -> None:
        oneDim = _barSpec(kind=ChartType.HEATMAP, dimensions=(_dim(),))
        assert validateSpec(oneDim, _COLUMNS) is not None

        twoDim = _barSpec(
            kind=ChartType.HEATMAP,
            dimensions=(_dim(), SpecDimension(field="LINE_AMT", label="金额")),
        )
        assert validateSpec(twoDim, _COLUMNS) is None

    def test_scatter_requires_two_measures(self) -> None:
        oneMeasure = _barSpec(kind=ChartType.SCATTER)
        assert validateSpec(oneMeasure, _COLUMNS) is not None

        twoMeasure = _barSpec(
            kind=ChartType.SCATTER,
            measures=(_measure(), _measure(field="LINE_AMT")),
        )
        assert validateSpec(twoMeasure, _COLUMNS) is None

    def test_kpi_requires_payload(self) -> None:
        spec = _barSpec(kind=ChartType.KPI, kpi=None)
        assert validateSpec(spec, _COLUMNS) is not None

        ok = _barSpec(
            kind=ChartType.KPI,
            kpi=SpecKpi(label="供货量", value=1234, unit="件"),
        )
        assert validateSpec(ok, _COLUMNS) is None


class TestCoerceDegradesToTableNeverEmpty:
    def test_valid_spec_passes_through_untouched(self) -> None:
        spec = _barSpec()

        coerced, reason = coerceSpec(spec, _COLUMNS)

        assert coerced is spec
        assert reason is None

    def test_broken_spec_degrades_to_table_with_reason(self) -> None:
        spec = _barSpec(dimensions=(_dim("NOT_A_COLUMN"),))

        coerced, reason = coerceSpec(spec, _COLUMNS)

        assert coerced.kind is ChartType.TABLE
        assert coerced.columns == tuple(_COLUMNS)
        assert reason is not None and reason != ""

    @pytest.mark.parametrize(
        "kind",
        [
            ChartType.LINE,
            ChartType.BAR,
            ChartType.HBAR,
            ChartType.PIE,
            ChartType.DONUT,
            ChartType.SCATTER,
            ChartType.HEATMAP,
            ChartType.KPI,
            ChartType.COMBO,
            ChartType.WATERFALL,
        ],
    )
    def test_every_chart_kind_degrades_rather_than_failing(self, kind: ChartType) -> None:
        """任何 kind 的坏 spec 都必须落到 TABLE —— 参数化保证新增 kind 时不会漏。"""
        broken = _barSpec(kind=kind, dimensions=(_dim("NOT_A_COLUMN"),))

        coerced, reason = coerceSpec(broken, _COLUMNS)

        assert coerced.kind is ChartType.TABLE
        assert reason is not None


class TestTableSpec:
    def test_table_spec_carries_columns_and_no_series(self) -> None:
        spec = tableSpec(_COLUMNS)

        assert spec.kind is ChartType.TABLE
        assert spec.columns == tuple(_COLUMNS)
        assert spec.series == ()
        assert spec.dimensions == ()
        assert spec.measures == ()

    def test_table_spec_survives_validation_with_empty_data(self) -> None:
        assert validateSpec(tableSpec([]), []) is None
