"""ChartService 单元测试。

覆盖：
- recommendChartType：各列组合规则（1 字符串+1 数值、时间+数值、2+字符串+数值、默认 TABLE）
- _fallbackOption：TABLE/PIE/BAR/LINE 的 ECharts option 结构
- generateChartOption：LLM 成功、非 JSON 回退、无 series 回退、抛异常回退
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.domain.enums import ChartType
from app.services.chart_service import ChartService


def _llmConfig() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model")


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 7
        self.completionTokens = 3


class _FakeLlm:
    def __init__(self, content: str, *, raises: bool = False) -> None:
        self._content = content
        self._raises = raises
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        if self._raises:
            raise RuntimeError("llm down")
        return _Resp(self._content)


def _rows(strings: list[str], numbers: list[int | Decimal]) -> list[dict]:
    return [{"NAME": n, "QTY": Decimal(q)} for n, q in zip(strings, numbers, strict=True)]


class TestRecommendChartType:
    def test_one_string_one_number_few_rows_is_pie(self) -> None:
        data = _rows(["A", "B", "C"], [1, 2, 3])
        assert ChartService().recommendChartType(["NAME", "QTY"], data) is ChartType.PIE

    def test_one_string_one_number_many_rows_is_bar(self) -> None:
        data = _rows([f"S{i}" for i in range(8)], [i for i in range(8)])
        assert ChartService().recommendChartType(["NAME", "QTY"], data) is ChartType.BAR

    def test_time_and_number_is_line(self) -> None:
        data = [{"D": "2026-08-01", "QTY": Decimal(1)}, {"D": "2026-08-02", "QTY": Decimal(2)}]
        assert ChartService().recommendChartType(["D", "QTY"], data) is ChartType.LINE

    def test_two_strings_one_number_is_bar(self) -> None:
        data = [
            {"CITY": "上海", "WAREHOUSE": "A", "QTY": Decimal(1)},
            {"CITY": "上海", "WAREHOUSE": "B", "QTY": Decimal(2)},
        ]
        assert ChartService().recommendChartType(["CITY", "WAREHOUSE", "QTY"], data) is ChartType.BAR

    def test_no_data_is_table(self) -> None:
        assert ChartService().recommendChartType(["NAME", "QTY"], []) is ChartType.TABLE

    def test_string_only_is_table(self) -> None:
        data = [{"NAME": "A"}]
        assert ChartService().recommendChartType(["NAME"], data) is ChartType.TABLE


class TestFallbackOption:
    def test_table_option_contains_columns_and_rows(self) -> None:
        data = _rows(["A"], [1])
        option = ChartService()._fallbackOption(ChartType.TABLE, ["NAME", "QTY"], data)
        assert option["columns"] == ["NAME", "QTY"]
        assert option["rows"] == data

    def test_pie_option_structure(self) -> None:
        data = _rows(["A", "B"], [1, 2])
        option = ChartService()._fallbackOption(ChartType.PIE, ["NAME", "QTY"], data)
        series = option["series"][0]
        assert series["type"] == "pie"
        assert series["data"] == [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]

    def test_bar_option_structure(self) -> None:
        data = _rows(["A", "B"], [1, 2])
        option = ChartService()._fallbackOption(ChartType.BAR, ["NAME", "QTY"], data)
        assert option["xAxis"]["data"] == ["A", "B"]
        series = option["series"][0]
        assert series["type"] == "bar"
        assert series["data"] == ["1", "2"]

    def test_line_option_structure(self) -> None:
        data = [{"D": "2026-08-01", "QTY": Decimal(1)}, {"D": "2026-08-02", "QTY": Decimal(2)}]
        option = ChartService()._fallbackOption(ChartType.LINE, ["D", "QTY"], data)
        assert option["series"][0]["type"] == "line"
        assert option["xAxis"]["data"] == ["2026-08-01", "2026-08-02"]


class TestGenerateChartOption:
    async def test_llm_success_returns_parsed_option(self) -> None:
        fake = _FakeLlm('{"series": [{"type": "bar", "data": [1]}]}')
        option, promptTokens, completionTokens = await ChartService().generateChartOption(
            ChartType.BAR, ["NAME", "QTY"], _rows(["A"], [1]), "问题", fake, _llmConfig()
        )
        assert option["series"][0]["type"] == "bar"
        assert promptTokens == 7
        assert completionTokens == 3

    async def test_llm_non_json_falls_back(self) -> None:
        fake = _FakeLlm("抱歉，我不能生成图表配置。")
        service = ChartService()
        data = _rows(["A", "B"], [1, 2])
        option, _, _ = await service.generateChartOption(
            ChartType.BAR, ["NAME", "QTY"], data, "问题", fake, _llmConfig()
        )
        assert option["series"][0]["type"] == "bar"
        assert option["xAxis"]["data"] == ["A", "B"]

    async def test_llm_json_without_series_falls_back(self) -> None:
        fake = _FakeLlm('{"title": {"text": "no series"}}')
        data = _rows(["A"], [1])
        option, _, _ = await ChartService().generateChartOption(
            ChartType.PIE, ["NAME", "QTY"], data, "问题", fake, _llmConfig()
        )
        assert option["series"][0]["type"] == "pie"

    async def test_llm_raises_falls_back(self) -> None:
        fake = _FakeLlm("", raises=True)
        data = _rows(["A"], [1])
        option, _, _ = await ChartService().generateChartOption(
            ChartType.LINE, ["D", "QTY"], [{"D": "2026-08-01", "QTY": Decimal(1)}], "问题", fake, _llmConfig()
        )
        assert option["series"][0]["type"] == "line"

    async def test_table_skips_llm_when_falls_back(self) -> None:
        fake = _FakeLlm("garbage")
        data = _rows(["A"], [1])
        option, _, _ = await ChartService().generateChartOption(
            ChartType.TABLE, ["NAME", "QTY"], data, "问题", fake, _llmConfig()
        )
        assert option["columns"] == ["NAME", "QTY"]
