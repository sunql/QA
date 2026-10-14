"""chat_multistep rewrite hook 单测。

验证 _executeDataStep 改写器命中时 sub_question 被替换；其他模型不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest


@dataclass
class FakeStepPlan:
    sub_question: str


class TestRewriteHook:
    def test_qwen_comparison_sub_question_rewritten(self) -> None:
        """Qwen + 含「对照同地点」→ sub_question 被替换"""
        from app.services.step_subquestion_rewriter import (
            RewriteResult,
            StepSubquestionRewriter,
        )

        rw = StepSubquestionRewriter()
        plan = FakeStepPlan(sub_question="对照同地点其他供应商的供货量变化")
        result = rw.rewrite(
            sub_question=plan.sub_question,
            prev_results=(),
            model_name="Qwen3.8-27B-4bit",
        )
        assert result.rewritten is not None
        # 用 dataclasses.replace 模拟不可变替换
        import dataclasses
        new_plan = dataclasses.replace(plan, sub_question=result.rewritten)
        assert new_plan.sub_question != plan.sub_question
        assert "RCV_SITE_CODE" in new_plan.sub_question

    def test_deepseek_no_rewrite(self) -> None:
        from app.services.step_subquestion_rewriter import StepSubquestionRewriter
        rw = StepSubquestionRewriter()
        plan = FakeStepPlan(sub_question="对照同地点其他供应商的供货量变化")
        result = rw.rewrite(
            sub_question=plan.sub_question,
            prev_results=(),
            model_name="deepseek-chat",
        )
        assert result.rewritten is None

    def test_qwen_non_comparison_no_rewrite(self) -> None:
        """Qwen 但子问题不含对照/对比/地点 → 不改写"""
        from app.services.step_subquestion_rewriter import StepSubquestionRewriter
        rw = StepSubquestionRewriter()
        plan = FakeStepPlan(sub_question="按月统计供货量趋势")
        result = rw.rewrite(
            sub_question=plan.sub_question,
            prev_results=(),
            model_name="Qwen3.8-27B",
        )
        assert result.rewritten is None
