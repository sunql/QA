"""L1 KPI 直答的指标卡（决策 7）。

L1 命中不调 LLM，直接执行 `kpi_catalog.formula` 拿一个标量。此前这条路只回文本
（「指标「X」：0.954」），前端也就没图可画 —— 而单值恰恰是 KPI 卡的标准形态。

这里的契约有两条：

1. **发不发卡由「值能不能变成数字」决定**。前端渲染门放宽为 `Boolean(chartType)`
   之后，一张 `value=null` 的空卡比不发更糟：用户看到一个空壳还以为数据没算出来。
   此时回答文本里的口径说明才是用户要看的东西。
2. **卡上的值与回答文本里的值是同一个值**。两处都从同一行取「第一个非 None」，
   各写一遍 `next(...)` 迟早漂移；这个文件把两者钉在一起。

`_buildKpiChart` 复用统一渲染器（`renderChartOption`），不手搓 `{"kpi": ...}` 结构 ——
KPI 负载的形状只有渲染器一个出口。
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import ChartType
from app.services.chat_service import ChatService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KPI_NAME = "供应商及时交货率"
UNIT_PERCENT = "%"


def _kpi(rows: list[dict] | None, unit: str | None = UNIT_PERCENT):
    """跑被测函数，返回 (kind, option)。"""
    return ChatService._buildKpiChart(KPI_NAME, unit, rows)


def _kpiPayload(rows: list[dict] | None, unit: str | None = UNIT_PERCENT) -> dict:
    kind, option = _kpi(rows, unit)
    assert kind is ChartType.KPI, f"期望出指标卡，实际 {kind!r}"
    assert option is not None
    assert set(option) == {"kpi"}, f"KPI 负载只应有 kpi 键，实际 {sorted(option)}"
    return option["kpi"]


# ---------------------------------------------------------------------------
# 出卡
# ---------------------------------------------------------------------------


class TestSingleValueBecomesACard:
    """单行单值 → KPI 卡（决策 7 的正例）。"""

    def test_decimal_value_becomes_a_json_number(self) -> None:
        """NUMERIC 列在 asyncpg 里是 Decimal —— 必须转成 JSON 数值而不是字符串。

        前端 `Statistic` 拿到 "0.954" 会当字符串原样显示，千分位/精度格式化全失效。
        """
        payload = _kpiPayload([{"otd_rate": Decimal("0.954")}])

        assert payload["label"] == KPI_NAME
        assert payload["value"] == 0.954
        assert isinstance(payload["value"], float)

    def test_integer_value_is_left_as_an_integer(self) -> None:
        payload = _kpiPayload([{"rcv_qty": 9812}])

        assert payload["value"] == 9812

    def test_numeric_string_is_coerced(self) -> None:
        """有些 formula 走 `to_char`/拼接，回来的是字符串数字。"""
        payload = _kpiPayload([{"amt": "98120.5"}])

        assert payload["value"] == 98120.5

    def test_unit_comes_from_the_catalog(self) -> None:
        """单位只有 kpi_catalog 有（本体属性没有语义元数据），必须显式带下来。"""
        payload = _kpiPayload([{"otd_rate": 0.954}], unit="%")

        assert payload["unit"] == "%"

    def test_delta_is_reserved_and_none_in_phase_one(self) -> None:
        """同环比差值一期不做 —— 字段必须存在且为 None，不能省（前端按字段存在性渲染）。"""
        payload = _kpiPayload([{"otd_rate": 0.954}])

        assert payload["delta"] is None

    def test_first_present_value_wins_when_the_row_has_several_columns(self) -> None:
        payload = _kpiPayload([{"a": None, "b": 42}])

        assert payload["value"] == 42

    def test_only_the_first_row_is_read(self) -> None:
        """与 `_buildAnswerText` 同口径：只看第一行。"""
        payload = _kpiPayload([{"v": 1}, {"v": 2}])

        assert payload["value"] == 1


# ---------------------------------------------------------------------------
# 不发卡（降级）
# ---------------------------------------------------------------------------


class TestNoValueMeansNoCard:
    """没有可展示的值就不发图 —— 空卡比无卡更糟。"""

    def test_missing_data_emits_no_chart(self) -> None:
        """formula 执行失败时 `_executeCalculationLogic` 返回 None。"""
        assert _kpi(None) == (None, None)

    def test_empty_rows_emit_no_chart(self) -> None:
        assert _kpi([]) == (None, None)

    def test_all_none_row_emits_no_chart(self) -> None:
        assert _kpi([{"otd_rate": None}]) == (None, None)

    def test_non_numeric_value_emits_no_chart(self) -> None:
        """`SELECT '暂无数据'` 这类占位值：回答文本照旧显示，但不发一张空壳卡。"""
        assert _kpi([{"note": "暂无数据"}]) == (None, None)

    def test_boolean_is_not_a_number(self) -> None:
        """`toNumber` 特意排除 bool（Python 里 True 是 int）。"""
        assert _kpi([{"flag": True}]) == (None, None)


# ---------------------------------------------------------------------------
# 文本与卡片同源
# ---------------------------------------------------------------------------


class TestCardAndAnswerAgree:
    """回答文本里的值与卡上的值是同一个 —— 这是两处各自的 `next(...)` 会漂移的地方。"""

    class _Kpi:
        """`_buildAnswerText` 只读 business_definition，给个最小替身即可。"""

        business_definition = "供应商按时交货的订单占比"

    def test_card_value_matches_the_answer_text(self) -> None:
        rows = [{"otd_rate": Decimal("0.954")}]
        text = ChatService._buildAnswerText(self._Kpi(), rows, KPI_NAME)
        payload = _kpiPayload(rows)

        assert text == f"指标「{KPI_NAME}」：0.954"
        assert str(payload["value"]) in text

    def test_missing_value_keeps_the_definition_in_the_text(self) -> None:
        """无 data → 文本回落到口径说明；此时也没有卡片，两者一致。"""
        text = ChatService._buildAnswerText(self._Kpi(), None, KPI_NAME)
        kind, option = _kpi(None)

        assert text == f"指标「{KPI_NAME}」（{self._Kpi.business_definition}）"
        assert (kind, option) == (None, None)

    def test_present_but_none_value_still_shows_the_placeholder_in_the_text(self) -> None:
        """老的 `：—` 行为保持不变（文本层的兜底），只是不再配一张卡。"""
        text = ChatService._buildAnswerText(self._Kpi(), [{"otd_rate": None}], KPI_NAME)

        assert text == f"指标「{KPI_NAME}」：—"
