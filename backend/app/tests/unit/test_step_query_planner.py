"""StepQueryPlanner 拆步判定逻辑单元测试。

新策略（单步优先）：
- is_explicit_multi_step()：识别"明确要求分步执行"的信号（分步/逐步/拆步/第一步等）。
- plan()：始终调用 LLM 结构化拆步（不再关键词快路径），仅在"明确分步"或"单步失败
  回退"两种场景由 ChatService 触发；拆出单步或失败时返回 None。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.step_query_planner import StepQueryPlanner


class _MockLlmClient:
    """按预设 content 返回的假 LLM 客户端，记录调用次数。"""

    def __init__(self, response_content: str) -> None:
        self.response_content = response_content
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1

        class _Resp:
            content: str
            modelName: str = "mock"
            promptTokens: int = 10
            completionTokens: int = 5
            totalTokens: int = 15

            def __init__(self, c: str) -> None:
                self.content = c

        return _Resp(self.response_content)


class TestExplicitMultiStep:
    """is_explicit_multi_step：明确分步信号判定（零 LLM 成本）。"""

    @pytest.mark.parametrize("question", [
        "请分步查询 2024 和 2025 年的销售额",
        "逐步统计各门店营收并对比",
        "一步步查一下最近三个月的订单",
        "把这个查询拆步执行",
        "第一步查各供应商，第二步做对比",
        "先查 2024 年销售额，再查 2025 年，对比给出趋势",   # 顺序指令：先…再…
        "先找到采购金额最多的 20 种物料，然后分析其每月变化趋势",  # 顺序指令：先…然后…
        "先看各门店营收，接着对比同比变化",
    ])
    def test_explicit_signal_true(self, question: str) -> None:
        assert StepQueryPlanner.is_explicit_multi_step(question) is True

    @pytest.mark.parametrize("question", [
        "对比 2024 和 2025 年的销售额",   # 对比动词，但未明确要求分步 → 先单步
        "2024 年 和 2025 年的销量",
        "各供应商的收货数量汇总",
        "什么是销售额",
        "你好",
        "再次查询各供应商的收货数量",   # 单个「再」无「先」不触发
        "随后按金额排序",               # 单个「随后」无「先」不触发
    ])
    def test_no_explicit_signal_false(self, question: str) -> None:
        assert StepQueryPlanner.is_explicit_multi_step(question) is False


class TestStepQueryPlannerPlan:
    """plan()：始终调用 LLM 拆步（无关键词快路径）。"""

    async def test_plan_always_calls_llm(self) -> None:
        """即使问题无显式多步信号，plan() 也调用 LLM 拆步（调用方已决定要拆）。"""
        planner = StepQueryPlanner()
        mock_client = _MockLlmClient('{"isMultiStep": false}')
        result = await planner.plan("各供应商的收货数量汇总", MagicMock(), mock_client, "gpt-4o")
        assert mock_client.calls == 1  # 不再有关键词快路径，始终调用 LLM
        assert result.plan is None  # LLM 返回 false → 计划为 None
        assert result.prompt_tokens == 10  # 调用已发生，token 仍随结果返回供计量
        assert result.completion_tokens == 5

    @pytest.mark.parametrize("question", [
        "对比 2024 和 2025 年的销售额",
        "比较各年份的营收",
    ])
    async def test_llm_returns_multi_step_true(self, question: str) -> None:
        """LLM 返回 isMultiStep=true + steps>=2：创建 MultiStepPlan。"""
        planner = StepQueryPlanner()
        mock_client = _MockLlmClient(
            '{"isMultiStep": true, '
            '"steps": ['
            '{"description": "2024 年销售额", "subQuestion": "2024年的销售额是多少"}, '
            '{"description": "2025 年销售额", "subQuestion": "2025年的销售额是多少"}'
            '], '
            '"aggregationHint": "对比两年数据给出趋势"}'
        )

        result = await planner.plan(question, MagicMock(), mock_client, "gpt-4o")
        assert result.plan is not None
        assert result.plan.is_single_step is False
        assert len(result.plan.steps) == 3  # 2 数据步 + 1 汇总步
        assert result.plan.aggregation_hint == "对比两年数据给出趋势"
        assert result.plan.original_question == question
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 5

    async def test_llm_returns_single_step(self) -> None:
        """LLM 返回 isMultiStep=true 但 steps=1：返回 None。"""
        planner = StepQueryPlanner()
        mock_client = _MockLlmClient(
            '{"isMultiStep": true, "steps": [{"description": "销售额", "subQuestion": "销售额是多少"}]}'
        )
        result = await planner.plan("对比销售额", MagicMock(), mock_client, "gpt-4o")
        assert result.plan is None
        assert result.prompt_tokens == 10  # 调用已发生，token 仍计量

    async def test_llm_returns_non_json(self) -> None:
        """LLM 返回非 JSON：返回 None，不抛异常。"""
        planner = StepQueryPlanner()
        mock_client = _MockLlmClient("这不是 JSON 响应")
        result = await planner.plan("对比各年销售额", MagicMock(), mock_client, "gpt-4o")
        assert result.plan is None

    async def test_llm_json_missing_isMultiStep(self) -> None:
        """LLM JSON 缺少 isMultiStep 字段：返回 None。"""
        planner = StepQueryPlanner()
        mock_client = _MockLlmClient('{"steps": []}')
        result = await planner.plan("对比各年销售额", MagicMock(), mock_client, "gpt-4o")
        assert result.plan is None

    async def test_llm_complete_raises(self) -> None:
        """LLM 调用抛异常：返回 None，不抛异常，token 记 0。"""
        planner = StepQueryPlanner()
        broken_client = AsyncMock()
        broken_client.complete.side_effect = RuntimeError("LLM 网络错误")
        result = await planner.plan("对比各年销售额", MagicMock(), broken_client, "gpt-4o")
        assert result.plan is None
        assert result.prompt_tokens == 0  # 调用未成功，无可计量 token
        assert result.completion_tokens == 0

    async def test_steps_over_max_limit_truncated(self) -> None:
        """拆出 6 个数据步骤（>MAX_MULTI_STEP-1=4）时截断到 4。"""
        planner = StepQueryPlanner()
        many_steps = ', '.join(
            f'{{"description": "step{i}", "subQuestion": "step{i}问"}}'
            for i in range(6)
        )
        mock_client = _MockLlmClient(
            f'{{"isMultiStep": true, "steps": [{many_steps}], "aggregationHint": "汇总"}}'
        )
        result = await planner.plan("对比各年销售额", MagicMock(), mock_client, "gpt-4o")
        assert result.plan is not None
        # MAX_MULTI_STEP=5，含汇总步骤最多5步 → 4数据步+1汇总步
        non_agg = [s for s in result.plan.steps if not s.aggregation_only]
        assert len(non_agg) == 4  # 截断到 4


class TestStepQueryPlannerExtractJson:
    """JSON 提取容错性。"""

    @pytest.mark.parametrize("raw", [
        '{"isMultiStep": true, "steps": []}',  # 干净 JSON
        '```json\n{"isMultiStep": true, "steps": []}\n```',  # 有围栏
        '以下是分析：\n{"isMultiStep": true, "steps": []}\n完毕',  # 前后有文本
        '  {"isMultiStep": true, "steps": []}  ',  # 空白
    ])
    async def test_extract_json_from_various_formats(self, raw: str) -> None:
        planner = StepQueryPlanner()
        result = planner._extract_json(raw)
        assert result is not None
        assert result.get("isMultiStep") is True

    async def test_extract_json_invalid(self) -> None:
        planner = StepQueryPlanner()
        assert planner._extract_json("not json at all") is None
        assert planner._extract_json("") is None


class TestRuleBasedSplit:
    """rule_based_split / plan_explicit：第X步标号直接规则拆分，零 LLM。

    修复「多步执行计划消失」回归（2026-08-16）：用户用「第X步」显式标号时，
    跳过 LLM 拆步判定，避免 LLM 错误返回 isMultiStep=false 导致多步被吞。
    """

    def test_chinese_numerals_split(self) -> None:
        q = "第一步查 2025 年采购，第二步查 2026 年同期，第三步对比趋势"
        plan = StepQueryPlanner.rule_based_split(q)
        assert plan is not None
        assert len(plan.steps) == 4  # 3 数据步 + 1 汇总
        assert plan.steps[0].sub_question.startswith("查 2025")
        assert plan.steps[-1].aggregation_only is True
        assert plan.is_single_step is False

    def test_arabic_numerals_split(self) -> None:
        plan = StepQueryPlanner.rule_based_split("第1步查 A，第2步查 B，第3步对比")
        assert plan is not None
        assert len(plan.steps) == 4

    def test_single_marker_returns_none(self) -> None:
        """仅 1 个「第X步」时不视为多步（保留给单步路径）。"""
        assert StepQueryPlanner.rule_based_split("第一步查采购数据") is None

    def test_no_marker_returns_none(self) -> None:
        """无「第X步」时返回 None（顺序指令走 LLM 路径）。"""
        assert StepQueryPlanner.rule_based_split("先查 X，再查 Y，对比趋势") is None

    def test_strips_punctuation(self) -> None:
        """步骤间标点（，,。、：； 全角 + 半角 + 全角空格）应被剥离。"""
        plan = StepQueryPlanner.rule_based_split("第一步查 A，第二步查 B，第三步对比。")
        assert plan is not None
        assert plan.steps[0].sub_question == "查 A"
        assert plan.steps[1].sub_question == "查 B"
        assert plan.steps[2].sub_question == "对比"

    def test_strips_full_width_punctuation(self) -> None:
        """全角标点（； ：、 。 ，）也应被剥离（2026-08-16 review 修复）。"""
        plan = StepQueryPlanner.rule_based_split("第一步查A；第二步查B：第三步对比。")
        assert plan is not None
        assert plan.steps[0].sub_question == "查A"
        assert plan.steps[1].sub_question == "查B"
        assert plan.steps[2].sub_question == "对比"

    def test_must_not_match_comparison_questions(self) -> None:
        """守约：对比/比较类问题不误匹配（避免破坏 test_no_explicit_signal_false）。"""
        assert StepQueryPlanner.rule_based_split("对比 2024 和 2025 年的销售额") is None
        assert StepQueryPlanner.rule_based_split("再次查询各供应商的收货数量") is None
        assert StepQueryPlanner.rule_based_split("比较各年份的营收") is None

    # ---- 序数副词序列（首先/其次/再者/最后）-----

    def test_ordinal_sequence_shu_xian_ran_hou_splits_three_steps(self) -> None:
        """用户实际场景（2026-08-17 bug）：'首先...其次...然后...' 三步必须拆出。

        历史 bug：仅「第X步」标号走规则拆分；序数副词序列只能靠 LLM 拆步，
        但 LLM 倾向返回 isMultiStep=false 导致多步被静默吞掉，
        最终只回 step 1 数据（4月份统计）让用户误以为系统没分步执行。
        """
        q = (
            "首先统计4月份采购订单数量，"
            "其次统计4月份主要top10采购物料的占比，"
            "然后看这top10物料在5月份下的订单数量信息分析"
        )
        plan = StepQueryPlanner.rule_based_split(q)
        assert plan is not None
        assert plan.is_single_step is False
        # 3 数据步 + 1 汇总
        assert len(plan.steps) == 4
        assert plan.steps[0].sub_question == "统计4月份采购订单数量"
        assert plan.steps[1].sub_question == "统计4月份主要top10采购物料的占比"
        assert plan.steps[2].sub_question == "看这top10物料在5月份下的订单数量信息分析"
        # 末尾追加自动汇总步骤
        assert plan.steps[-1].aggregation_only is True

    def test_ordinal_sequence_shu_xian_zai_zhe_zui_hou_splits_four_steps(self) -> None:
        """四步序数序列：'首先...其次...再者...最后...'。"""
        q = (
            "首先查供应商A的采购额，"
            "其次查供应商B的采购额，"
            "再者查供应商C的采购额，"
            "最后对比三者的差异"
        )
        plan = StepQueryPlanner.rule_based_split(q)
        assert plan is not None
        assert len(plan.steps) == 5  # 4 数据步 + 1 汇总
        assert plan.steps[0].sub_question == "查供应商A的采购额"
        assert plan.steps[3].sub_question == "对比三者的差异"

    def test_ordinal_sequence_qi_yi_qi_er_qi_san_splits_three_steps(self) -> None:
        """古文序数 '其一/其二/其三' 也应识别。"""
        q = (
            "其一查2023年订单，其二查2024年订单，其三查2025年订单"
        )
        plan = StepQueryPlanner.rule_based_split(q)
        assert plan is not None
        assert len(plan.steps) == 4
        assert plan.steps[0].sub_question == "查2023年订单"
        assert plan.steps[1].sub_question == "查2024年订单"
        assert plan.steps[2].sub_question == "查2025年订单"

    def test_single_ordinal_returns_none(self) -> None:
        """仅 1 个序数副词不视为多步（避免把单条问题误拆）。"""
        assert StepQueryPlanner.rule_based_split("首先看供应商A的收货数量") is None

    def test_comparison_with_ordinal_anchor_still_none(self) -> None:
        """对比类问题不因含'再次'而被误拆（'再次' 是 _RULE_STRIP_CHARS 邻居，单条仍非多步）。"""
        assert StepQueryPlanner.rule_based_split("再次查询各供应商的收货数量") is None

    def test_truncates_description_to_limit(self) -> None:
        """description 截断到 _RULE_DESCRIPTION_LIMIT 字符。"""
        long_chunk = "查询每个供应商的详细采购明细数据，包括物料名称、数量、单价、金额等"
        plan = StepQueryPlanner.rule_based_split(
            f"第一步{long_chunk}，第二步再查汇总"
        )
        assert plan is not None
        assert plan.steps[0].description.endswith("...")  # 截断标记
        assert len(plan.steps[0].description) <= 23  # 20 字符 + 3 省略号
        # sub_question 保留全文
        assert plan.steps[0].sub_question == long_chunk

    async def test_plan_explicit_returns_plan_no_llm_call(self) -> None:
        """第X步标号命中时 plan_explicit() 不调 LLM，token=0。"""
        planner = StepQueryPlanner()
        mock_client = AsyncMock()
        result = await planner.plan_explicit("第一步查 A，第二步查 B")
        mock_client.complete.assert_not_called()
        assert result.plan is not None
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0

    async def test_plan_explicit_returns_none_falls_back_to_plan(self) -> None:
        """无标号时 plan_explicit() 返回 None plan（调用方回退到 plan()）。"""
        planner = StepQueryPlanner()
        result = await planner.plan_explicit("对比 2024 和 2025 年的销售额")
        assert result.plan is None
        assert result.prompt_tokens == 0
