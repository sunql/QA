"""门面：决策 → spec → 渲染 的编排（含 LLM 调用次数与 token 口径）。

本文件取代旧版（旧版钉的是 `recommendChartType` / `generateChartOption` /
`_fallbackOption` 三个已删除的内部实现 —— 它们正是这轮改造要消灭的「只看形状 +
让 LLM 写 option」）。

**这里最该钉住的两件事**：
1. **LLM 调用次数**：确定性形状（占比有 formula、时间维趋势）一次都不该调；
   只有歧义形状才调一次。这是「不能完全让 LLM 判定」的量化落点。
2. **降级链**：任何异常/非法 spec 都落到表格，`chartType` 必须跟着变成 table
   （否则前端按 chartType 选了图表渲染器，却拿到表格负载 → 画空白）。
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import ChartType
from app.domain.query_plan import Aggregation, QueryPlan, SortSpec
from app.services.chart_service import ChartService

_ROWS = [
    {"SUPPLIER_NAME": "B125 浙江力航", "RCV_QTY_PUU": 9812},
    {"SUPPLIER_NAME": "B019 温州圣特", "RCV_QTY_PUU": 7401},
    {"SUPPLIER_NAME": "B153 天津精一", "RCV_QTY_PUU": 5203},
]
_MONTH_ROWS = [
    {"MONTH": "2026-01", "LINE_AMT": 100},
    {"MONTH": "2026-02", "LINE_AMT": 200},
]


class _FakeSession:
    """system_config 全缺席 → 阈值全走默认（fail-open）。"""

    async def execute(self, stmt: object) -> Any:
        class _R:
            def scalar_one_or_none(self_inner) -> None:
                return None

        return _R()


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.promptTokens = 42
        self.completionTokens = 1
        self.cachedTokens = 5


class _CountingClient:
    """记录调用次数 —— 「不调 LLM」和「调一次」都要能被断言。"""

    def __init__(self, content: str = "SHARE", boom: bool = False) -> None:
        self.content = content
        self.boom = boom
        self.calls = 0

    async def complete(self, messages: list[Any], **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        if self.boom:
            raise RuntimeError("LLM 502")
        return _FakeResponse(self.content)


class _FakeModelConfig:
    model_name = "deepseek-chat"


def _plan(**overrides) -> QueryPlan:
    base: dict = {"target": "DWD_GOODS_RECEIPT_DTL"}
    base.update(overrides)
    return QueryPlan(**base)


async def _build(
    *,
    columns: list[str],
    data: list[dict],
    plan: QueryPlan | None = None,
    question: str = "",
    forcedKind: ChartType | None = None,
    intentKind: ChartType | None = None,
    client: _CountingClient | None = None,
):
    return await ChartService().buildChart(
        session=_FakeSession(),
        plan=plan if plan is not None else _plan(),
        columns=columns,
        data=data,
        question=question,
        forcedKind=forcedKind,
        intentKind=intentKind,
        llmClient=client if client is not None else _CountingClient("COMPARE"),
        modelConfig=_FakeModelConfig(),
    )


class TestEmptyData:
    async def test_no_data_yields_table_payload_and_zero_usage(self) -> None:
        build = await _build(columns=["SUPPLIER_NAME", "RCV_QTY_PUU"], data=[])

        assert build.chartType is ChartType.TABLE
        assert build.option == {"columns": ["SUPPLIER_NAME", "RCV_QTY_PUU"], "rows": []}
        assert (build.promptTokens, build.completionTokens) == (0, 0)

    async def test_no_data_does_not_call_the_llm(self) -> None:
        client = _CountingClient()
        await _build(columns=["A"], data=[], client=client)
        assert client.calls == 0

    async def test_forced_kind_yields_to_empty_data(self) -> None:
        """没有数据就没有图：强制 heatmap 也返回表格，避免前端画空白。"""
        build = await _build(
            columns=["A", "B"], data=[], forcedKind=ChartType.HEATMAP
        )
        assert build.chartType is ChartType.TABLE
        assert "columns" in build.option


class TestDeterministicShapesSkipTheLlm:
    """规则能定下来的形状，一次 LLM 都不该调（「不能完全让 LLM 判定」的落点）。"""

    async def test_share_formula_is_donut_without_llm(self) -> None:
        plan = _plan(
            aggregations=(
                Aggregation(
                    function="SUM", property="RCV_QTY_PUU", alias="占比", formula="x"
                ),
            ),
            groupBy=("SUPPLIER_NAME",),
        )
        rows = [
            {"SUPPLIER_NAME": "B125", "占比": 0.5},
            {"SUPPLIER_NAME": "B019", "占比": 0.3},
            {"SUPPLIER_NAME": "B153", "占比": 0.2},
        ]
        client = _CountingClient()

        build = await _build(
            columns=["SUPPLIER_NAME", "占比"], data=rows, plan=plan, client=client
        )

        assert build.chartType is ChartType.DONUT
        assert client.calls == 0
        assert build.promptTokens == 0

    async def test_time_trend_is_line_without_llm(self) -> None:
        """问句说了「趋势」→ 确定性线索直接定音，一次分类调用都不该有。"""
        client = _CountingClient()
        build = await _build(
            columns=["MONTH", "LINE_AMT"],
            data=_MONTH_ROWS,
            plan=_plan(groupBy=("MONTH",)),
            question="按月的入库金额趋势",
            client=client,
        )

        assert build.chartType is ChartType.LINE
        assert client.calls == 0

    async def test_silent_trend_question_lets_the_classifier_flip_to_bar(self) -> None:
        """问句没线索 → 走一次分类；这正是歧义的定义（可被翻，但只翻在候选集内）。"""
        client = _CountingClient("COMPARE")

        build = await _build(
            columns=["MONTH", "LINE_AMT"],
            data=_MONTH_ROWS,
            plan=_plan(groupBy=("MONTH",)),
            client=client,
        )

        assert client.calls == 1
        assert build.chartType is ChartType.BAR

    async def test_silent_trend_with_an_unusable_label_keeps_line(self) -> None:
        client = _CountingClient("TREND")

        build = await _build(
            columns=["MONTH", "LINE_AMT"],
            data=_MONTH_ROWS,
            plan=_plan(groupBy=("MONTH",)),
            client=client,
        )

        assert build.chartType is ChartType.LINE

    async def test_topn_is_hbar_without_llm(self) -> None:
        client = _CountingClient()
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            plan=_plan(
                groupBy=("SUPPLIER_NAME",),
                sortBy=(SortSpec(property="RCV_QTY_PUU", direction="desc"),),
                rowLimit=3,
            ),
            client=client,
        )

        assert build.chartType is ChartType.HBAR
        assert client.calls == 0

    async def test_raw_detail_is_table_without_llm(self) -> None:
        client = _CountingClient()
        build = await _build(
            columns=["PO_NO", "SUPPLIER_NAME", "LINE_AMT"],
            data=[{"PO_NO": "P1", "SUPPLIER_NAME": "B125", "LINE_AMT": 10}],
            plan=_plan(),
            client=client,
        )

        assert build.chartType is ChartType.TABLE
        assert client.calls == 0


class TestAmbiguousShapeAsksOnce:
    def _ambiguous(self) -> dict:
        return {"columns": ["SUPPLIER_NAME", "RCV_QTY_PUU"], "data": _ROWS}

    async def test_ambiguous_shape_calls_the_classifier_exactly_once(self) -> None:
        client = _CountingClient("COMPARE")

        build = await _build(**self._ambiguous(), client=client)

        assert client.calls == 1
        assert build.chartType is ChartType.BAR
        assert build.decision.labelHint == "COMPARE"

    async def test_label_share_switches_to_donut(self) -> None:
        build = await _build(**self._ambiguous(), client=_CountingClient("SHARE"))
        assert build.chartType is ChartType.DONUT

    async def test_label_rank_switches_to_hbar(self) -> None:
        build = await _build(**self._ambiguous(), client=_CountingClient("RANK"))
        assert build.chartType is ChartType.HBAR

    async def test_garbage_label_keeps_the_rule_verdict(self) -> None:
        build = await _build(**self._ambiguous(), client=_CountingClient("hbar"))

        assert build.chartType is ChartType.BAR
        assert build.decision.labelHint is None

    async def test_llm_exception_keeps_the_rule_verdict_and_zero_usage(self) -> None:
        client = _CountingClient(boom=True)

        build = await _build(**self._ambiguous(), client=client)

        assert client.calls == 1
        assert build.chartType is ChartType.BAR
        assert (build.promptTokens, build.completionTokens, build.cachedTokens) == (0, 0, 0)

    async def test_token_triple_is_forwarded(self) -> None:
        build = await _build(**self._ambiguous(), client=_CountingClient("COMPARE"))

        assert build.promptTokens == 42
        assert build.completionTokens == 1
        assert build.cachedTokens == 5


class TestForcedKinds:
    async def test_forced_kind_wins_over_the_engine(self) -> None:
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            forcedKind=ChartType.PIE,
            client=_CountingClient(),
        )

        assert build.chartType is ChartType.PIE
        assert build.decision.ruleId == "R_FORCED_CLIENT"

    async def test_forced_kind_skips_the_classifier(self) -> None:
        client = _CountingClient()
        await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            forcedKind=ChartType.PIE,
            client=client,
        )
        assert client.calls == 0

    async def test_forced_kind_beats_intent_kind(self) -> None:
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            forcedKind=ChartType.PIE,
            intentKind=ChartType.LINE,
        )
        assert build.chartType is ChartType.PIE

    async def test_intent_kind_used_when_no_client_force(self) -> None:
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            intentKind=ChartType.LINE,
        )
        assert build.chartType is ChartType.LINE

    async def test_impossible_forced_kind_degrades_instead_of_blank(self) -> None:
        """在 1 维数据上强制热力图 → 确定性降级，而不是发一个画不出的图。

        降级终点是表格（`coerceSpec` 只做「合法则留、非法则退表格」，不做
        「猜一个相近的图型」）—— 猜错了同样是错，表格至少是诚实的。
        """
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            forcedKind=ChartType.HEATMAP,
        )

        assert build.chartType is ChartType.TABLE
        assert build.spec.kind is ChartType.TABLE
        assert build.option["columns"] == ["SUPPLIER_NAME", "RCV_QTY_PUU"]


class TestOptionIsRenderable:
    async def test_bar_option_carries_categories_and_series(self) -> None:
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            plan=_plan(groupBy=("SUPPLIER_NAME",)),
        )

        assert build.option["xAxis"]["data"] == ["B125 浙江力航", "B019 温州圣特", "B153 天津精一"]
        assert build.option["series"][0]["data"] == [9812, 7401, 5203]

    async def test_no_color_is_sent_to_the_frontend(self) -> None:
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            plan=_plan(groupBy=("SUPPLIER_NAME",)),
        )
        assert "color" not in build.option

    async def test_spec_is_returned_for_observability(self) -> None:
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            plan=_plan(groupBy=("SUPPLIER_NAME",)),
        )
        assert build.spec.kind is build.chartType
        assert build.spec.columns == ("SUPPLIER_NAME", "RCV_QTY_PUU")


class TestNeverRaises:
    async def test_plan_none_still_produces_a_chart(self) -> None:
        """多步的某些分支没有 plan —— 退化到「只看形状」也得出图，不能抛。"""
        build = await _build(
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"], data=_ROWS, plan=None
        )

        assert build.chartType in set(ChartType)
        assert isinstance(build.option, dict)

    async def test_session_failure_does_not_break_the_chart(self) -> None:
        """system_config 抖动不该让图消失（阈值读取 fail-open）。

        断言的是**不变量**：读不到阈值时选出的 kind 与读到默认值时完全一致。
        硬编码某个 kind 只能证明「这次没崩」，证明不了「降级到默认值」。
        """

        class _BoomSession:
            async def execute(self, stmt: object) -> Any:
                raise RuntimeError("UndefinedTableError: system_config")

        async def _run(session: Any):
            return await ChartService().buildChart(
                session=session,
                plan=_plan(groupBy=("SUPPLIER_NAME",)),
                columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
                data=_ROWS,
                question="",
                llmClient=_CountingClient("COMPARE"),
                modelConfig=_FakeModelConfig(),
            )

        healthy = await _run(_FakeSession())
        broken = await _run(_BoomSession())

        assert broken.chartType is healthy.chartType
        assert broken.option == healthy.option

    async def test_no_llm_client_still_works(self) -> None:
        """llmClient=None（如多步预算耗尽）→ 走规则判定，不抛。"""
        build = await ChartService().buildChart(
            session=_FakeSession(),
            plan=_plan(),
            columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
            data=_ROWS,
            question="",
            llmClient=None,
            modelConfig=None,
        )

        assert build.chartType is ChartType.BAR
        assert (build.promptTokens, build.completionTokens) == (0, 0)
