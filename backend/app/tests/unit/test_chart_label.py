"""消歧分类器：让 LLM 只回答一个**语义标签**，不回答图型、不写渲染代码。

**为什么值得让 LLM 参与**：规则能判形状，判不了意图。「供应商 + 数量」这三行数据
在占比问句和分类比较问句下**形状完全一样**，只有语义能分开。这是 LLM 的唯一职责，
也是它唯一能出错的地方 —— 所以错误必须被限制在「选了个不合适的图」，而不是
「图渲染不出来」。

**四条失败路径都必须安全**：异常、超时、不在白名单、返回图型名 —— 一律返回
`label=None`，由调用方沿用规则原判。LLM 挂了绝不影响出图。

**prompt 只带 ≤5 行样本**：分类只需要形状直觉，把 500 行数据塞进去只是烧 token。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.chart_label import (
    LABEL_SAMPLE_ROWS,
    SEMANTIC_LABELS,
    LabelResult,
    classifySemanticLabel,
)

_ROWS = [
    {"SUPPLIER_NAME": f"B{i:03d}", "RCV_QTY_PUU": i * 100} for i in range(30)
]


class _FakeResponse:
    def __init__(self, content: str, cachedTokens: int | None = 7) -> None:
        self.content = content
        self.promptTokens = 111
        self.completionTokens = 3
        self.cachedTokens = cachedTokens


class _FakeClient:
    """记录最后一次 messages，便于对 prompt 做断言。"""

    def __init__(self, content: str = "COMPARE", boom: bool = False) -> None:
        self.content = content
        self.boom = boom
        self.lastMessages: list[Any] = []
        self.lastModel: str | None = None

    async def complete(self, messages: list[Any], **kwargs: Any) -> _FakeResponse:
        self.lastMessages = messages
        self.lastModel = kwargs.get("model")
        if self.boom:
            raise RuntimeError("LLM 网关 502")
        return _FakeResponse(self.content)


class _FakeModelConfig:
    model_name = "deepseek-chat"


async def _classify(content: str, question: str = "各供应商供货量占比"):
    client = _FakeClient(content)
    result = await classifySemanticLabel(
        question=question,
        columns=["SUPPLIER_NAME", "RCV_QTY_PUU"],
        data=_ROWS,
        llmClient=client,
        modelConfig=_FakeModelConfig(),
    )
    return result, client


class TestWhitelistParsing:
    @pytest.mark.parametrize("label", sorted(SEMANTIC_LABELS))
    async def test_every_whitelisted_label_is_accepted(self, label: str) -> None:
        result, _ = await _classify(label)
        assert result.label == label

    async def test_lowercase_is_normalized(self) -> None:
        result, _ = await _classify("share")
        assert result.label == "SHARE"

    async def test_surrounding_whitespace_is_tolerated(self) -> None:
        result, _ = await _classify("  RANK\n")
        assert result.label == "RANK"

    async def test_code_fence_is_stripped(self) -> None:
        """LLM 常把单值也裹进 ``` 围栏；不剥就永远解析失败。"""
        result, _ = await _classify("```\nSHARE\n```")
        assert result.label == "SHARE"

    async def test_trailing_punctuation_is_tolerated(self) -> None:
        result, _ = await _classify("COMPARE.")
        assert result.label == "COMPARE"


class TestRejections:
    async def test_chart_type_name_is_rejected(self) -> None:
        """LLM 想直接给图型 —— 契约不允许，按解析失败处理（规则原判接管）。"""
        for bad in ("hbar", "pie", "heatmap", "bar"):
            result, _ = await _classify(bad)
            assert result.label is None, f"{bad} 不该被接受"

    async def test_free_text_is_rejected(self) -> None:
        result, _ = await _classify("我建议用饼图来展示这个占比")
        assert result.label is None

    async def test_empty_response_is_rejected(self) -> None:
        result, _ = await _classify("")
        assert result.label is None

    async def test_echarts_fragment_is_rejected(self) -> None:
        result, _ = await _classify('{"series": [{"type": "pie"}]}')
        assert result.label is None


class TestFailurePathsAreSafe:
    async def test_exception_returns_no_label_and_zero_usage(self) -> None:
        result = await classifySemanticLabel(
            question="q",
            columns=["A"],
            data=[{"A": 1}],
            llmClient=_FakeClient(boom=True),
            modelConfig=_FakeModelConfig(),
        )

        assert result == LabelResult(
            label=None, promptTokens=0, completionTokens=0, cachedTokens=0
        )

    async def test_rejection_still_reports_usage(self) -> None:
        """解析失败但调用成功 —— token 花了，账要记（否则计量口径漏账）。"""
        result, _ = await _classify("hbar")

        assert result.label is None
        assert result.promptTokens == 111
        assert result.completionTokens == 3
        assert result.cachedTokens == 7

    async def test_cached_tokens_are_passed_through(self) -> None:
        client = _FakeClient("SHARE")
        result = await classifySemanticLabel(
            question="q",
            columns=["A"],
            data=[{"A": 1}],
            llmClient=client,
            modelConfig=_FakeModelConfig(),
        )
        assert result.cachedTokens == 7


class TestPrompt:
    async def test_prompt_carries_the_question_and_columns(self) -> None:
        _, client = await _classify("COMPARE", question="哪些供应商供货最多")

        userContent = client.lastMessages[-1].content
        assert "哪些供应商供货最多" in userContent
        assert "SUPPLIER_NAME" in userContent
        assert "RCV_QTY_PUU" in userContent

    async def test_prompt_dumps_only_a_few_sample_rows(self) -> None:
        """30 行数据只许带 ≤5 行样本进去（分类不需要全量，全量只是烧 token）。"""
        _, client = await _classify("COMPARE")

        userContent = client.lastMessages[-1].content
        assert f"B00{LABEL_SAMPLE_ROWS}" not in userContent, "样本行数超了"
        assert "B029" not in userContent, "把全量数据塞进 prompt 了"

    async def test_system_prompt_forbids_chart_types_and_code(self) -> None:
        _, client = await _classify("COMPARE")

        system = client.lastMessages[0].content
        assert "一个词" in system or "只输出" in system
        assert "ECharts" in system

    async def test_model_name_is_forwarded(self) -> None:
        _, client = await _classify("COMPARE")
        assert client.lastModel == "deepseek-chat"

    async def test_prompt_lists_every_allowed_label(self) -> None:
        """白名单必须写进 prompt，否则模型只能猜（猜错就是解析失败）。"""
        _, client = await _classify("COMPARE")

        userContent = client.lastMessages[-1].content
        for label in SEMANTIC_LABELS:
            assert label in userContent, f"prompt 缺少标签 {label}"
