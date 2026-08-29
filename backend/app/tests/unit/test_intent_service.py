"""IntentService 单元测试：关键词命中 / 短消息 / 正常查询 / 领域命令与实体抽取。"""

from __future__ import annotations

import pytest

from app.domain.enums import ChartType, IntentType
from app.services.intent_service import IntentService


class TestIntentService:
    @pytest.fixture()
    def service(self) -> IntentService:
        return IntentService()

    def test_chinese_greeting_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("你好") is IntentType.CHITCHAT

    def test_greeting_with_suffix_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("你好呀") is IntentType.CHITCHAT

    def test_thanks_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("谢谢") is IntentType.CHITCHAT

    def test_english_help_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("HELP me") is IntentType.CHITCHAT

    def test_hello_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("Hello") is IntentType.CHITCHAT

    def test_short_message_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("查") is IntentType.CHITCHAT

    def test_empty_message_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("   ") is IntentType.CHITCHAT

    def test_query_returns_query(self, service: IntentService) -> None:
        assert service.classify("各供应商的收货数量汇总") is IntentType.QUERY

    def test_query_with_whitespace_returns_query(self, service: IntentService) -> None:
        assert service.classify("  查询上个月的销售总额  ") is IntentType.QUERY

    def test_what_can_you_do_returns_chitchat(self, service: IntentService) -> None:
        assert service.classify("能做什么") is IntentType.CHITCHAT

    # =========================================================================
    # 多轮意图：REFINE / FOLLOW_UP / CLARIFY / NEW_QUERY（Phase C）
    # =========================================================================

    def test_refine_sort_with_prior_state(self, service: IntentService) -> None:
        assert service.classify("按数量降序排序", hasPriorState=True) is IntentType.REFINE

    def test_refine_row_limit_with_prior_state(self, service: IntentService) -> None:
        assert service.classify("只看前10条", hasPriorState=True) is IntentType.REFINE

    def test_refine_filter_with_prior_state(self, service: IntentService) -> None:
        assert service.classify("筛选收货数量大于100的", hasPriorState=True) is IntentType.REFINE

    def test_refine_without_prior_state_is_query(self, service: IntentService) -> None:
        # 无历史状态时"排序"等调整词不具备 REFINE 语义，回退为全新查询
        assert service.classify("按数量排序", hasPriorState=False) is IntentType.QUERY

    # ===== 2026-08-17 回归：显式分步问题不被 REFINE/FOLLOW_UP 抢走 =====

    def test_explicit_multi_step_not_refine_even_with_topn(self, service: IntentService) -> None:
        """显式分步问题含 top10（命中 _REFINE_LIMIT_RE），有历史状态也必须是 NEW_QUERY。

        真实回归：'第一步…第二步…top10…占比' 被判 REFINE，chat_service 多步入口
        只认 NEW_QUERY/QUERY -> 完全绕过多步路径，单步只算了订单数量。
        """
        q = (
            "第一步统计3月份采购订单数量，输出列表，"
            "第二步统计3月份主要top10采购物料的占比，输出饼图，"
            "第三步分析这top10物料在4月份下的订单数量信息分析"
        )
        assert service.classify(q, hasPriorState=True) is IntentType.NEW_QUERY

    def test_ordinal_multi_step_not_refine(self, service: IntentService) -> None:
        """序数副词分步（首先…其次…）含'占比'等 FOLLOW_UP 关键词，仍走 NEW_QUERY。"""
        q = "首先统计3月份订单数量，其次统计top10物料的占比"
        assert service.classify(q, hasPriorState=True) is IntentType.NEW_QUERY

    def test_single_step_reference_keeps_refine(self, service: IntentService) -> None:
        """仅 1 个「第X步」指代（对上一轮多步结果的微调）仍是 REFINE。"""
        q = "把第一步的结果按金额降序排序"
        assert service.classify(q, hasPriorState=True) is IntentType.REFINE

    def test_single_ordinal_not_followup_suppressed(self, service: IntentService) -> None:
        """单「继续」承接 + 无分步标号：FOLLOW_UP 语义保留（守约既有行为）。"""
        assert service.classify("继续看下个月的数据", hasPriorState=True) is IntentType.FOLLOW_UP

    def test_follow_up_referent_with_prior_state(self, service: IntentService) -> None:
        assert service.classify("它占了多少比例", hasPriorState=True) is IntentType.FOLLOW_UP

    def test_follow_up_without_prior_state_is_query(self, service: IntentService) -> None:
        assert service.classify("为什么A公司最多", hasPriorState=False) is IntentType.QUERY

    # ===== 3-2 / N6：FOLLOW_UP 收紧，泛化关键词不再把新查询锚定到旧上下文 =====

    def test_follow_up_requires_referent(self, service: IntentService) -> None:
        """"为什么A公司最多"无指代词，是有前置状态的新查询而非追问，走 NEW_QUERY。"""
        assert service.classify("为什么A公司最多", hasPriorState=True) is IntentType.NEW_QUERY

    def test_distribution_query_not_anchored(self, service: IntentService) -> None:
        """"分别统计各月的销售额"含"分别"但无指代词，是全新查询，不被旧上下文锚定。"""
        assert service.classify("分别统计各月的销售额", hasPriorState=True) is IntentType.NEW_QUERY

    def test_follow_up_with_referent_and_keyword(self, service: IntentService) -> None:
        """同时命中指代词与关键词（它/这个 + 为什么/哪个）才是 FOLLOW_UP。"""
        assert service.classify("它为什么最多", hasPriorState=True) is IntentType.FOLLOW_UP
        assert service.classify("这个月最高的是哪个", hasPriorState=True) is IntentType.FOLLOW_UP

    def test_continuation_words_stay_follow_up(self, service: IntentService) -> None:
        """纯承接词（继续/接着）本身即指代上一轮，无需再带指代词。"""
        assert service.classify("继续看下个月的数据", hasPriorState=True) is IntentType.FOLLOW_UP
        assert service.classify("接着看下个月的", hasPriorState=True) is IntentType.FOLLOW_UP

    def test_haiyou_lingwai_not_continuation(self, service: IntentService) -> None:
        """"还有库存""另外的仓库"是全新查询，"还有/另外"歧义大，不再按承接词锚定。"""
        assert service.classify("哪些产品还有库存", hasPriorState=True) is IntentType.NEW_QUERY
        assert service.classify("另外的仓库数据", hasPriorState=True) is IntentType.NEW_QUERY

    def test_qita_is_not_referent(self, service: IntentService) -> None:
        """"其他产品哪个库存最多"里"其他"的"它"是形容词，不是指代词，不锚定。"""
        result = service.classify("其他产品哪个库存最多", hasPriorState=True)
        assert result is IntentType.NEW_QUERY

    def test_clarify_without_prior_state(self, service: IntentService) -> None:
        assert service.classify("收货数量是什么意思") is IntentType.CLARIFY

    def test_clarify_precedes_refine(self, service: IntentService) -> None:
        # "排序是什么意思" 命中 REFINE 与 CLARIFY 双关键词，含义优先
        assert service.classify("排序是什么意思", hasPriorState=True) is IntentType.CLARIFY

    # ===== 3-1 / N5：CLARIFY 收紧，实体查询不再误判为概念解释 =====

    def test_superlative_entity_query_is_not_clarify(self, service: IntentService) -> None:
        """"最高的是什么产品"问实体（哪款产品最高），收紧后不再是 CLARIFY。"""
        assert service.classify("最高的是什么产品", hasPriorState=False) is IntentType.QUERY
        assert service.classify("最高的是什么产品", hasPriorState=True) is not IntentType.CLARIFY

    def test_superlative_trailing_what_is_not_clarify(self, service: IntentService) -> None:
        """以"是什么"结尾但含最高级的实体查询（"销售额最高的是什么"）仍降级 QUERY。"""
        assert service.classify("销售额最高的是什么", hasPriorState=False) is IntentType.QUERY
        assert service.classify("销售额最高的是什么", hasPriorState=True) is not IntentType.CLARIFY

    def test_bare_term_what_is_is_clarify(self, service: IntentService) -> None:
        """"周转率是什么"（术语+是什么，无最高级）仍是概念解释。"""
        assert service.classify("周转率是什么") is IntentType.CLARIFY
        assert service.classify("这个指标是什么") is IntentType.CLARIFY

    def test_what_is_prefix_with_superlative_is_not_clarify(self, service: IntentService) -> None:
        """"什么是最畅销的产品"问实体，前缀"什么是"也套用最高级排除，走 QUERY。"""
        assert service.classify("什么是最畅销的产品", hasPriorState=False) is IntentType.QUERY
        assert service.classify("什么是最畅销的产品", hasPriorState=True) is not IntentType.CLARIFY

    def test_difference_query_needs_he_connector(self, service: IntentService) -> None:
        """区别类 CLARIFY 需"和"连接（毛利和净利区别）；无"和"的数据比较走 QUERY。"""
        assert service.classify("毛利和净利的区别") is IntentType.CLARIFY
        assert service.classify("各供应商销售额的区别", hasPriorState=False) is IntentType.QUERY
        # "最大的区别是什么"是数据比较请求（哪两者差异最大），不是概念解释
        assert service.classify("最大的区别是什么", hasPriorState=False) is IntentType.QUERY

    def test_strict_clarify_forms_kept(self, service: IntentService) -> None:
        """明确的含义/解释句式保持不变。"""
        assert service.classify("毛利率是什么意思") is IntentType.CLARIFY
        assert service.classify("解释一下毛利率") is IntentType.CLARIFY
        assert service.classify("什么是毛利") is IntentType.CLARIFY
        assert service.classify("毛利和净利的区别") is IntentType.CLARIFY

    def test_new_query_with_prior_state(self, service: IntentService) -> None:
        assert service.classify("查询今年的采购总额", hasPriorState=True) is IntentType.NEW_QUERY

    # =========================================================================
    # 领域命令意图：DEFINE / MAP / METRIC（Phase 2 接入）
    # =========================================================================

    def test_define_metric_extracts_name_and_formula(self, service: IntentService) -> None:
        result = service.classifyResult("定义指标 销售额 = SUM(order.amount)")
        assert result.intent is IntentType.DEFINE
        assert result.metric == "销售额"
        assert result.formula == "SUM(order.amount)"

    def test_define_metric_with_colon(self, service: IntentService) -> None:
        result = service.classifyResult("新增指标：order_count = COUNT(id)")
        assert result.intent is IntentType.DEFINE
        assert result.metric == "order_count"
        assert result.formula == "COUNT(id)"

    def test_define_without_formula(self, service: IntentService) -> None:
        result = service.classifyResult("定义指标")
        assert result.intent is IntentType.DEFINE
        assert result.metric is None
        assert result.formula is None

    def test_classify_delegates_to_define(self, service: IntentService) -> None:
        assert service.classify("定义指标 X = SUM(y)") is IntentType.DEFINE

    def test_map_extracts_source_and_target(self, service: IntentService) -> None:
        result = service.classifyResult("把 客户名称 映射到 客户类")
        assert result.intent is IntentType.MAP
        assert result.source == "客户名称"
        assert result.target == "客户类"

    def test_map_with_arrow(self, service: IntentService) -> None:
        result = service.classifyResult("订单金额 -> 订单")
        assert result.intent is IntentType.MAP
        assert result.source == "订单金额"
        assert result.target == "订单"

    def test_metric_intent_lists_metrics(self, service: IntentService) -> None:
        assert service.classifyResult("有哪些指标").intent is IntentType.METRIC

    def test_metric_intent_with_named_metric(self, service: IntentService) -> None:
        assert service.classifyResult("查看销售额指标").intent is IntentType.METRIC

    def test_metric_refine_takes_priority(self, service: IntentService) -> None:
        # "按指标排序" 含 REFINE 与 METRIC 双关键词，调整语义优先
        assert service.classifyResult("按指标排序", hasPriorState=True).intent is IntentType.REFINE

    def test_data_query_mentioning_metric_stays_query(self, service: IntentService) -> None:
        # 数据查询提到"指标"但无查询性提示词：不应误判为 METRIC
        assert service.classifyResult("各业务线的销售额指标汇总").intent is IntentType.QUERY

    # =========================================================================
    # 查询实体抽取：维度 / 指标 / 图表类型（best-effort）
    # =========================================================================

    def test_extracts_chart_type_bar(self, service: IntentService) -> None:
        result = service.classifyResult("用柱状图展示各供应商的销售额")
        assert result.intent is IntentType.QUERY
        assert result.chartType is ChartType.BAR

    def test_extracts_chart_type_pie(self, service: IntentService) -> None:
        assert service.classifyResult("画个饼图").chartType is ChartType.PIE

    def test_extracts_chart_type_line(self, service: IntentService) -> None:
        assert service.classifyResult("折线图展示趋势").chartType is ChartType.LINE

    def test_extracts_dimension_and_metric(self, service: IntentService) -> None:
        result = service.classifyResult("按地区汇总各供应商的销售额总额")
        assert result.intent is IntentType.QUERY
        assert result.dimension == "地区"
        assert result.metric == "销售额"

    def test_extracts_dimension_with_each(self, service: IntentService) -> None:
        result = service.classifyResult("每个供应商的收货数量汇总")
        assert result.dimension == "供应商"

    def test_metric_phrase_before_total_word(self, service: IntentService) -> None:
        result = service.classifyResult("查询今年的采购总额")
        assert result.metric == "采购"

    def test_query_without_entities(self, service: IntentService) -> None:
        result = service.classifyResult("查询上个月的销售情况")
        assert result.intent is IntentType.QUERY
        assert result.dimension is None
        assert result.metric is None
        assert result.chartType is None

    def test_refine_extracts_entities(self, service: IntentService) -> None:
        result = service.classifyResult("筛选按地区汇总销售额总额", hasPriorState=True)
        assert result.intent is IntentType.REFINE
        assert result.dimension == "地区"

    # ===== 斜杠指令（Phase 5）=====

    def test_slash_metric_extracts_name_and_formula(self, service: IntentService) -> None:
        result = service.classifyResult("/metric 销售额 = SUM(order.amount)")
        assert result.intent is IntentType.METRIC
        assert result.metric == "销售额"
        assert result.formula == "SUM(order.amount)"

    def test_slash_define_extracts_class_name(self, service: IntentService) -> None:
        result = service.classifyResult("/define 产品")
        assert result.intent is IntentType.DEFINE
        assert result.target == "产品"

    def test_slash_define_with_alias_and_desc(self, service: IntentService) -> None:
        result = service.classifyResult("/define 产品 alias=Product desc=销售商品")
        assert result.intent is IntentType.DEFINE
        assert result.target == "产品"
        assert result.source == "Product"
        assert result.formula == "销售商品"

    def test_slash_define_without_name_is_metric_fallback(self, service: IntentService) -> None:
        # `/define` 后无类名 → 不匹配斜杠指令，落回自然语言（不命中任何关键词→QUERY）
        result = service.classifyResult("/define")
        assert result.intent is IntentType.QUERY

    def test_slash_map_extracts_source_and_target(self, service: IntentService) -> None:
        result = service.classifyResult("/map 客户ID -> 客户")
        assert result.intent is IntentType.MAP
        assert result.source == "客户ID"
        assert result.target == "客户"

    def test_slash_takes_priority_over_natural_language(self, service: IntentService) -> None:
        # `/metric` 优先于 "定义指标" 关键词
        result = service.classifyResult("/metric 销售额 = SUM(x)")
        assert result.intent is IntentType.METRIC
        assert result.formula is not None

    def test_slash_metric_without_formula_still_metric(self, service: IntentService) -> None:
        # 仅有名称无公式仍归 METRIC（实际公式校验交给 _handleDefineMetric）
        result = service.classifyResult("/metric 客单价")
        assert result.intent is IntentType.METRIC
        assert result.metric == "客单价"
        assert result.formula is None
