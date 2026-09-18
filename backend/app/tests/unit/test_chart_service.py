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


class TestBuildOptionPromptSmallData:
    """v2 2026-09-18：chart LLM prompt 在数据量小（≤ FULL_DATA_THRESHOLD）时全量展示。

    触发：用户报告 27 行时 chart_service._buildOptionPrompt 的 data[:20]
    让 LLM 生成的 ECharts option 不含 D1 后几年 → 前端 EVENT_CHART.data 27 行
    按 option 渲染时 D1 数据「越界」或裁掉。

    修复：≤ FULL_DATA_THRESHOLD 行（默认 100）时全量嵌入 prompt。
    """

    async def test_27_rows_includes_all_rows_in_prompt(self) -> None:
        """用户真实场景：27 行（B019+B125+D1）必须全在 LLM prompt 里。"""
        from app.services.data_summary import FULL_DATA_THRESHOLD
        rows = [{"供应商": f"S{i:03d}", "数量": i * 10} for i in range(27)]
        fake = _FakeLlm('{"series": [{"type": "bar", "data": []}]}')
        await ChartService().generateChartOption(
            ChartType.BAR, ["供应商", "数量"], rows, "问题", fake, _llmConfig(),
        )
        # 取最后一次调用的 user prompt（generateChartOption 只发一次）
        userPrompt = fake.calls[0][1][1]
        # 最后一行的供应商名 S026 必须出现（全量嵌入）；旧实现 data[:20] 不会含 S026
        assert "S026" in userPrompt
        # 行数标注应说"27 行"，不是"最多 20 行"
        assert f"共 {27} 行" in userPrompt
        assert "最多 20 行" not in userPrompt

    async def test_at_threshold_includes_all_rows(self) -> None:
        """边界：恰好 100 行全量嵌入。"""
        from app.services.data_summary import FULL_DATA_THRESHOLD
        rows = [{"i": i, "label": f"L{i:03d}"} for i in range(FULL_DATA_THRESHOLD)]
        fake = _FakeLlm('{"series": [{"type": "bar", "data": []}]}')
        await ChartService().generateChartOption(
            ChartType.BAR, ["i", "label"], rows, "问题", fake, _llmConfig(),
        )
        userPrompt = fake.calls[0][1][1]
        # 最后一行的 label 必须出现
        assert f"L{FULL_DATA_THRESHOLD - 1:03d}" in userPrompt

    async def test_over_threshold_truncates_to_20(self) -> None:
        """边界：101 行退回到 data[:20]，prompt 不含第 21 行。"""
        from app.services.data_summary import FULL_DATA_THRESHOLD
        rows = [{"i": i, "label": f"L{i:03d}"} for i in range(FULL_DATA_THRESHOLD + 1)]
        fake = _FakeLlm('{"series": [{"type": "bar", "data": []}]}')
        await ChartService().generateChartOption(
            ChartType.BAR, ["i", "label"], rows, "问题", fake, _llmConfig(),
        )
        userPrompt = fake.calls[0][1][1]
        # L020（索引 20，第 21 行）不应在 prompt 中
        assert "L020" not in userPrompt
        # L000-L019 应在
        assert "L000" in userPrompt
        assert "L019" in userPrompt
        # 标记"最多 20 行"
        assert "最多 20 行" in userPrompt


class TestNormalizeOptionFormatters:
    """v3 2026-09-18：LLM 生成的 ECharts option 归一化 formatter 模板。

    触发：用户报告问题 #1 柱状图 label 显示字面量 `{d}%`、tooltip 仅 B019 有值。
    根因：LLM 写了 `'{d}%'` 意图「数值 + 百分号」，但 ECharts `{d}` 仅 pie 百分比。
    修复：`_normalizeOptionFormatters` 在非 pie 场景下把 `{d}` 替换为 `{c}`（数值）。
    """

    def test_bar_label_d_replaced_with_c(self) -> None:
        """柱图 label.formatter `{d}%` → `{c}%`。"""
        option = {"series": [{"type": "bar", "label": {"formatter": "{d}%"}}]}
        out = ChartService._normalizeOptionFormatters(option, ChartType.BAR)
        assert out["series"][0]["label"]["formatter"] == "{c}%"

    def test_pie_label_d_unchanged(self) -> None:
        """饼图 label.formatter `{d}%` 保持原样（标准用法）。"""
        option = {"series": [{"type": "pie", "label": {"formatter": "{d}%"}}]}
        out = ChartService._normalizeOptionFormatters(option, ChartType.PIE)
        assert out["series"][0]["label"]["formatter"] == "{d}%"

    def test_line_tooltip_d_replaced(self) -> None:
        """线图 tooltip.formatter 含 `{d}` → 替换为 `{c}`。"""
        option = {"tooltip": {"formatter": "{a}<br>{b}: {d}%"}}
        out = ChartService._normalizeOptionFormatters(option, ChartType.LINE)
        assert out["tooltip"]["formatter"] == "{a}<br>{b}: {c}%"

    def test_function_formatter_unchanged(self) -> None:
        """函数 formatter 不动（LLM 写函数时意图明确）。"""
        fn = lambda params: f"{params.value}%"  # noqa: E731
        option = {"series": [{"label": {"formatter": fn}}]}
        out = ChartService._normalizeOptionFormatters(option, ChartType.BAR)
        assert out["series"][0]["label"]["formatter"] is fn

    def test_nested_tooltip_inside_series_replaced(self) -> None:
        """series[i].tooltip.formatter 也归一化（用户报告 tooltip 仅 B019 有值的根因）。"""
        option = {"series": [{"type": "bar", "tooltip": {"formatter": "{a}: {d}"}}]}
        out = ChartService._normalizeOptionFormatters(option, ChartType.BAR)
        assert out["series"][0]["tooltip"]["formatter"] == "{a}: {c}"

    def test_immutability(self) -> None:
        """必须返回新对象，原 option 不被修改（遵循 CLAUDE.md 不可变数据）。"""
        option = {"series": [{"label": {"formatter": "{d}"}}]}
        ChartService._normalizeOptionFormatters(option, ChartType.BAR)
        # 原对象原样保留
        assert option["series"][0]["label"]["formatter"] == "{d}"


class TestGenerateChartOptionNormalizeIntegration:
    """v3 端到端：LLM 响应含 `{d}%` → generateChartOption 归一化后 series.label.formatter = `{c}%`。"""

    async def test_llm_d_percent_formatter_normalized(self) -> None:
        from app.domain.enums import ChartType
        fake = _FakeLlm('{"title": {"text": "x"}, "tooltip": {"trigger": "axis"}, '
                        '"xAxis": {"type": "category", "data": ["A", "B"]}, '
                        '"yAxis": {"type": "value"}, '
                        '"series": [{"type": "bar", "data": [10, 20], '
                        '"label": {"show": true, "formatter": "{d}%"}}]}')
        option, _, _ = await ChartService().generateChartOption(
            ChartType.BAR, ["name", "value"], _rows(["A", "B"], [10, 20]),
            "占比", fake, _llmConfig(),
        )
        # normalize 后 formatter 应该是 `{c}%`，不再是字面量 `{d}%`
        assert option["series"][0]["label"]["formatter"] == "{c}%"
