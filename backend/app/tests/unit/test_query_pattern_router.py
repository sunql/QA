"""QueryPatternRouter 单测。"""

from __future__ import annotations

import pytest

from app.services.query_pattern_router import (
    QueryPatternRouter,
    RouteHint,
    RouteAction,
    QueryPattern,
)


class TestSingleStepComparison:
    """单步对比不应被识别为多步复合题（避免误判 bypass Qwen）。"""

    def test_single_step_comparison_returns_none(self) -> None:
        router = QueryPatternRouter()
        hint = router.route("对比 A 和 B 的金额", is_multi_step=False)
        assert hint.forced_model_id is None
        assert hint.reason == ""


class TestMultiStepWithoutComparison:
    """多步但无对比 → 不强制 deepseek。"""

    def test_multi_step_no_comparison_returns_none(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：第一步查 X 的金额，第二步查 Y 的金额",
            is_multi_step=True,
        )
        assert hint.forced_model_id is None


class TestMultiStepWithComparison:
    """多步 + 对比关键词 → 强制 deepseek。"""

    def test_multi_step_comparison_zhengdui(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步分析：B019 圣特公司近 12 个月供货量下降的原因。"
            "第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，"
            "第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_multi_step_comparison_duibi(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：第一步查 3 月供货量，第二步对比 B019 和 B125 的月供货量",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_multi_step_comparison_vs(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：先查 A 数据, 然后 B vs C 同期对比",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_multi_step_panduan(self) -> None:
        router = QueryPatternRouter()
        hint = router.route(
            "分步：先查 X，然后查 Y，判断是季节因素还是结构因素",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID


class TestEdgeCases:
    """降级 / 边界。"""

    def test_empty_question_returns_none(self) -> None:
        router = QueryPatternRouter()
        assert router.route("", is_multi_step=True).forced_model_id is None

    def test_only_zhengdui_no_other_keyword_returns_none(self) -> None:
        """只含「对照」无「判断是/vs/对比」中的任何一个 → 不强制"""
        router = QueryPatternRouter()
        hint = router.route("对照清单查一下", is_multi_step=True)
        assert hint.forced_model_id is None

    def test_case_insensitive_match(self) -> None:
        """关键词不区分大小写"""
        router = QueryPatternRouter()
        hint = router.route(
            "分步：第一步查 X, 第二步 VS A 和 B",
            is_multi_step=True,
        )
        assert hint.forced_model_id == RouteAction.FORCE_DEEPSEEK_MODEL_ID

    def test_pattern_table_is_data_driven(self) -> None:
        """_PATTERNS 列表必须可枚举 + QueryPattern dataclass 字段稳定"""
        assert len(QueryPatternRouter._PATTERNS) >= 1
        for p in QueryPatternRouter._PATTERNS:
            assert isinstance(p.id, str) and p.id
            assert isinstance(p.keywords_all, tuple) and p.keywords_all
            assert isinstance(p.keywords_any, tuple) and p.keywords_any
            assert isinstance(p.action, RouteAction)


class TestRouteAction:
    def test_force_deepseek_model_id_is_1(self) -> None:
        """deepseek-chat 在 seed_models.py 是 id=1。SSOT 由 seed 定义。"""
        assert RouteAction.FORCE_DEEPSEEK_MODEL_ID == 1
