"""StepSubquestionRewriter 单测。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.multi_step_plan import StepResult
from app.services.step_subquestion_rewriter import (
    RewriteResult,
    RewriteRule,
    StepSubquestionRewriter,
)


def _make_prev_results(supplier_codes: list[str]) -> tuple[StepResult, ...]:
    """构造一个含 supplier_codes 的前序 StepResult（仅用于变量抽取）。"""
    from dataclasses import dataclass, field
    return ()  # 实际抽取逻辑只看 prev_results[i].aggregate 文本


class TestModelFilter:
    """仅 Qwen 系列生效。"""

    def test_deepseek_no_rewrite(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商",
            prev_results=(),
            model_name="deepseek-chat",
        )
        assert result.rewritten is None

    def test_qwen_with_match_triggers_rewrite(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商的供货量变化",
            prev_results=(),
            model_name="Qwen3.8-27B-4bit",
        )
        assert result.rewritten is not None
        assert "RCV_SITE_CODE" in result.rewritten

    def test_qwen_lowercase_match(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商",
            prev_results=(),
            model_name="qwen2.5:7b",
        )
        assert result.rewritten is not None


class TestPatternMatch:
    """sub_question 文本必须命中规则才改写。"""

    def test_no_keyword_no_rewrite(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="按月统计供货量趋势",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None

    def test_only_zhengdui_no_location_no_rewrite(self) -> None:
        """含「对照」但无「地点」 → 不命中 compare_by_location 规则"""
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照三个供应商的金额",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None


class TestTemplateExtraction:
    """模板变量从 prev_results 中抽取。"""

    def test_supplier_extracted_from_prev_aggregate(self) -> None:
        """prev_results 含 B019 → 改写后文本含 'B019'"""
        rw = StepSubquestionRewriter()
        # Mock StepResult with aggregate text
        prev = (
            _mock_step_result(
                aggregate_text="B019 各月供货量: 100, 200, 150",
            ),
        )
        result = rw.rewrite(
            sub_question="对照同地点其他供应商的供货量变化",
            prev_results=prev,
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is not None
        assert "B019" in result.rewritten

    def test_no_prev_uses_default(self) -> None:
        """prev_results 为空 → 仍能改写（不要供应商提取的前置依赖）"""
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question="对照同地点其他供应商的供货量变化",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is not None


class TestDegradation:
    """异常 / 边界降级。"""

    def test_none_sub_question_returns_none(self) -> None:
        rw = StepSubquestionRewriter()
        result = rw.rewrite(
            sub_question=None,  # type: ignore[arg-type]
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None

    def test_extractor_exception_returns_none(self) -> None:
        """提取器抛异常 → 降级为不重写"""
        rw = StepSubquestionRewriter()

        # Monkey-patch 提取器抛异常
        class BoomRule(RewriteRule):
            def extract_vars(self, prev_results):  # type: ignore[override]
                raise RuntimeError("boom")

        rw._RULES = (BoomRule(
                id="boom",
                match=lambda sq, prev: True,
                template="x {supplier}",
                extract_vars=lambda prev: {"supplier": "B019"},
            ),)
        result = rw.rewrite(
            sub_question="对照同地点其他供应商",
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None


def _mock_step_result(aggregate_text: str) -> StepResult:
    """构造仅含 aggregate_text 字段的 StepResult mock。"""
    sr = MagicMock(spec=StepResult)
    sr.aggregate_text = aggregate_text  # 提取器读 aggregate_text
    return sr  # type: ignore[return-value]


class TestRewriteRuleDataDriven:
    def test_rule_fields(self) -> None:
        """RewriteRule 必须满足字段约束"""
        assert len(StepSubquestionRewriter._RULES) >= 1
        for r in StepSubquestionRewriter._RULES:
            assert isinstance(r.id, str) and r.id
            assert callable(r.match)
            assert isinstance(r.template, str) and r.template
            assert callable(r.extract_vars)
