"""问题范围感知行数限制（scope-aware row limit）单元测试。

覆盖：
- _hasTimeScope：时间词正则命中与防误判
- _hasExplicitRowIntent：显式条数/最值意图识别
- _coerceRowLimit：脏值（字符串/负数/布尔/对象）归一为正整数或 None
- _applyScopeRowLimit：决策表四档（无法回答/显式条数/有范围/聚合/无范围兜底，默认关闭）
- 不可变性：返回新 plan 且其他字段无损
- defaultLimit 配置开关生效（NL2SQL_NO_SCOPE_ROW_LIMIT 默认 0）

设计依据：changes/feat-scope-aware-row-limit/summary.md §5 边界决策表。
默认行为变更（2026-08-19）：取消"无范围明细查询"的强制 100 行兜底，行数交回 LLM 判断。
"""

from __future__ import annotations

from app.domain.query_plan import Aggregation, JoinSpec, QueryPlan, SortSpec
from app.services.nl2sql_service import (
    _applyScopeRowLimit,
    _coerceRowLimit,
    _hasExplicitRowIntent,
    _hasTimeScope,
)


# ============================================================================
# _hasTimeScope：时间范围词识别
# ============================================================================


class TestHasTimeScope:
    def test_matches_year_with_chinese_suffix(self) -> None:
        assert _hasTimeScope("2025年的收货明细") is True

    def test_matches_year_month_with_dash(self) -> None:
        assert _hasTimeScope("2025-05 采购") is True

    def test_matches_year_month_with_slash(self) -> None:
        assert _hasTimeScope("查 2025/05/01 的收货") is True

    def test_matches_chinese_numerals_year(self) -> None:
        # 二〇二四年 / 二零二四年：CJK 数字
        assert _hasTimeScope("二〇二四年采购额") is True
        assert _hasTimeScope("二零二四年采购额") is True

    def test_matches_digit_month(self) -> None:
        # "5月" 命中，"3个月均价" 必须不命中（"个"阻断）
        assert _hasTimeScope("5月采购量") is True
        assert _hasTimeScope("十二月的订单") is True

    def test_matches_quarter(self) -> None:
        assert _hasTimeScope("Q1 采购") is True
        assert _hasTimeScope("第一季度采购") is True
        assert _hasTimeScope("一季度采购") is True

    def test_matches_relative_time(self) -> None:
        for text in (
            "上月采购额",
            "今年的采购",
            "去年同期",
            "近 30 天",
            "最近 3 个月",
            "今天的订单",
            "截至目前",
        ):
            assert _hasTimeScope(text) is True, text

    # ---- 反例：不应误判 ----

    def test_does_not_match_unscoped_summary(self) -> None:
        # 业务核心场景：用户汇总查询，无时间词，应判无范围
        assert _hasTimeScope("各供应商的收货数量汇总") is False

    def test_does_not_match_list_all(self) -> None:
        # 需求保护场景："列出所有X" 不应被误判为有范围
        assert _hasTimeScope("列出所有收货记录") is False
        assert _hasTimeScope("列出所有供应商") is False

    def test_does_not_match_time_bucket_only(self) -> None:
        # "按月" 是分组粒度词，不是范围（见 _TIME_BUCKET_TOKENS 注释）
        assert _hasTimeScope("按月统计采购趋势") is False
        assert _hasTimeScope("按年份分组对比") is False

    def test_does_not_match_quantity_modifier(self) -> None:
        # "3个月均价" 中"个"阻断（"3 个月" ≠ 范围词）
        assert _hasTimeScope("3个月均价") is False

    def test_does_not_match_top_n(self) -> None:
        assert _hasTimeScope("采购额最高的物料") is False
        assert _hasTimeScope("前10条收货记录") is False

    def test_returns_false_for_empty_or_none(self) -> None:
        assert _hasTimeScope("") is False


# ============================================================================
# _hasExplicitRowIntent：显式条数/最值意图
# ============================================================================


class TestHasExplicitRowIntent:
    def test_matches_explicit_top_n(self) -> None:
        # 复用 _extractLimit（与 REFINE 捷径同口径）
        assert _hasExplicitRowIntent("前 10 条收货记录") is True
        assert _hasExplicitRowIntent("top 5 供应商") is True
        assert _hasExplicitRowIntent("限 3 个") is True

    def test_matches_superlative_keywords(self) -> None:
        assert _hasExplicitRowIntent("采购额最高的供应商") is True
        assert _hasExplicitRowIntent("销量最低的产品") is True
        assert _hasExplicitRowIntent("最新订单") is True
        assert _hasExplicitRowIntent("排名前几的供应商") is True

    def test_does_not_match_unscoped_summary(self) -> None:
        # 关键回归：纯汇总查询不应被识别为"用户已表达条数意图"
        assert _hasExplicitRowIntent("各供应商采购汇总") is False


# ============================================================================
# _coerceRowLimit：脏值归一
# ============================================================================


class TestCoerceRowLimit:
    def test_passes_through_positive_int(self) -> None:
        assert _coerceRowLimit(100) == 100
        assert _coerceRowLimit(1) == 1

    def test_none_stays_none(self) -> None:
        assert _coerceRowLimit(None) is None

    def test_coerces_numeric_string(self) -> None:
        # QueryPlan.from_dict 对 rowLimit 不做类型校验，模型可能给 "100"
        assert _coerceRowLimit("100") == 100
        assert _coerceRowLimit("42") == 42

    def test_rejects_non_positive(self) -> None:
        assert _coerceRowLimit(0) is None
        assert _coerceRowLimit(-5) is None
        assert _coerceRowLimit("-1") is None
        assert _coerceRowLimit("0") is None

    def test_rejects_bool(self) -> None:
        # bool 是 int 的子类，必须显式排除
        assert _coerceRowLimit(True) is None
        assert _coerceRowLimit(False) is None

    def test_rejects_other_types(self) -> None:
        assert _coerceRowLimit({"a": 1}) is None
        assert _coerceRowLimit([100]) is None
        assert _coerceRowLimit(1.5) is None  # 浮点拒绝（行数必须整数）
        assert _coerceRowLimit("abc") is None


# ============================================================================
# _applyScopeRowLimit：四档决策表
# ============================================================================


def _detailPlan(rowLimit: int | None = None) -> QueryPlan:
    """构造一个"无聚合无分组的明细查询"计划（规则 4 兜底场景）。"""
    return QueryPlan(
        target="收货明细",
        selectedClasses=("PRECEIPT",),
        selectedProperties=("PTHNUM", "QTY"),
        rowLimit=rowLimit,
    )


def _aggregatePlan(rowLimit: int | None = None) -> QueryPlan:
    """构造"按供应商汇总"聚合计划（规则 3 场景）。"""
    return QueryPlan(
        target="各供应商收货数量",
        selectedClasses=("PRECEIPT",),
        selectedProperties=("BPSNUM", "QTY"),
        aggregations=(Aggregation(function="SUM", property="QTY", alias="TOTAL_QTY"),),
        groupBy=("BPSNUM",),
        rowLimit=rowLimit,
    )


class TestApplyScopeRowLimitDetailBranch:
    """规则 4：无范围 + 明细计划 → 取决于 NL2SQL_NO_SCOPE_ROW_LIMIT 配置。

    默认（0）关闭兜底，rowLimit 保持原值；配置为正整数时生效（向后兼容老行为）。
    """

    def test_unscoped_detail_disabled_by_default(self) -> None:
        # 默认 nl2sqlNoScopeRowLimit=0：关闭兜底，rowLimit 保持 None
        plan = _detailPlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        assert result.rowLimit is None

    def test_unscoped_detail_with_existing_100_is_idempotent(self) -> None:
        # LLM 已写 100 → 保持 100（默认关闭兜底时不会注入新值）
        plan = _detailPlan(rowLimit=100)
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        assert result.rowLimit == 100

    def test_unscoped_detail_with_excessive_row_limit_not_clamped_by_default(self) -> None:
        # 默认关闭兜底：LLM 写 999999 不再被钳制到 100
        plan = _detailPlan(rowLimit=999999)
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        assert result.rowLimit == 999999

    def test_default_limit_zero_disables_fallback(self) -> None:
        # 配置开关：NL2SQL_NO_SCOPE_ROW_LIMIT=0 时不注入兜底
        plan = _detailPlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "列出所有收货记录", defaultLimit=0)
        assert result.rowLimit is None

    def test_default_limit_positive_applies_fallback(self) -> None:
        # 配置开启（defaultLimit=100）：无范围明细查询被强制注入 100
        plan = _detailPlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "列出所有收货记录", defaultLimit=100)
        assert result.rowLimit == 100


class TestApplyScopeRowLimitScopeBranch:
    """规则 2：有范围 → 不限制（None）。"""

    def test_time_scope_clears_row_limit(self) -> None:
        plan = _detailPlan(rowLimit=100)
        result = _applyScopeRowLimit(plan, "2025年的收货明细")
        assert result.rowLimit is None

    def test_conditions_count_as_scope(self) -> None:
        # 问题无时间词但计划带 conditions → 同样判为有范围
        plan = QueryPlan(
            target="查询",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            conditions=("供应商 = 'S1'",),
            rowLimit=100,
        )
        result = _applyScopeRowLimit(plan, "采购量")
        assert result.rowLimit is None

    def test_scope_with_existing_null_is_idempotent(self) -> None:
        plan = _detailPlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "2025年的收货明细")
        assert result.rowLimit is None

    def test_scope_takes_priority_over_aggregation_branch(self) -> None:
        # 即使有聚合分组，有范围时仍清空 rowLimit
        plan = _aggregatePlan(rowLimit=100)
        result = _applyScopeRowLimit(plan, "2025年各供应商收货数量")
        assert result.rowLimit is None


class TestApplyScopeRowLimitExplicitRowBranch:
    """规则 1：用户已表达条数/最值意图 → 不介入。"""

    def test_explicit_top_n_is_preserved(self) -> None:
        # 关键回归：不得把用户要的 10 覆盖成 100
        plan = _detailPlan(rowLimit=10)
        result = _applyScopeRowLimit(plan, "前 10 条收货记录")
        assert result.rowLimit == 10

    def test_superlative_query_preserves_row_limit(self) -> None:
        plan = _detailPlan(rowLimit=1)
        result = _applyScopeRowLimit(plan, "采购额最高的供应商")
        assert result.rowLimit == 1


class TestApplyScopeRowLimitAggregationBranch:
    """规则 3：有聚合/分组 → 不注入兜底（截断分组会产生错误结论）。"""

    def test_groupby_plan_keeps_null(self) -> None:
        plan = _aggregatePlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "各供应商收货数量汇总")
        assert result.rowLimit is None


class TestApplyScopeRowLimitUnanswerable:
    """规则 0：target=无法回答 → 原样返回。"""

    def test_unanswerable_plan_returns_as_is(self) -> None:
        plan = QueryPlan(target="无法回答", rowLimit=None)
        result = _applyScopeRowLimit(plan, "列出所有订单")
        assert result is plan
        assert result.rowLimit is None


class TestApplyScopeRowLimitImmutability:
    """frozen dataclass 不可变性：返回新 plan，原 plan 不变。"""

    def test_returns_same_plan_when_disabled(self) -> None:
        # 默认 NL2SQL_NO_SCOPE_ROW_LIMIT=0：无范围 + 明细计划 → 不需要修改 rowLimit，直接返回原 plan
        plan = _detailPlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        assert result is plan
        # 原 plan 未变
        assert plan.rowLimit is None

    def test_returns_new_plan_when_fallback_active(self) -> None:
        # 配置开启（defaultLimit=100）：无范围 + 明细 + rowLimit=None → 注入 100，返回新 plan
        plan = _detailPlan(rowLimit=None)
        result = _applyScopeRowLimit(plan, "列出所有收货记录", defaultLimit=100)
        assert result is not plan
        assert result.rowLimit == 100
        # 原 plan 未变
        assert plan.rowLimit is None

    def test_preserves_other_fields(self) -> None:
        # 防 replace() 误用导致 interpretation/joins/sortBy 丢失
        # 注意：fixture 不带 aggregations/groupBy，否则会命中规则 3（保留模型值）而非规则 4
        # 默认 NL2SQL_NO_SCOPE_ROW_LIMIT=0 → rowLimit 保持 None
        join = JoinSpec(sourceClass="PRECEIPT", targetClass="PRECEIPT", columns=("PTHNUM",))
        sort = SortSpec(property="QTY", direction="desc")
        plan = QueryPlan(
            target="查询",
            selectedClasses=("PRECEIPT",),
            selectedProperties=("QTY",),
            joins=(join,),
            sortBy=(sort,),
            rowLimit=None,
            interpretation="用户想知道收货数量",
        )
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        assert result.rowLimit is None  # 默认关闭兜底，rowLimit 不变
        # 其他字段无损
        assert result.target == plan.target
        assert result.aggregations == plan.aggregations
        assert result.joins == plan.joins
        assert result.sortBy == plan.sortBy
        assert result.interpretation == plan.interpretation
        assert result.selectedClasses == plan.selectedClasses
        assert result.selectedProperties == plan.selectedProperties


class TestApplyScopeRowLimitDirtyInput:
    """plan.rowLimit 含脏值时也应归一处理。"""

    def test_dirty_row_limit_normalized_then_kept_when_disabled(self) -> None:
        # 模型给 "100" 字符串 + 无范围明细 → 归一为 100（默认关闭兜底，不再被覆盖）
        plan = _detailPlan(rowLimit="100")  # type: ignore[arg-type]
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        assert result.rowLimit == 100

    def test_dirty_negative_row_limit_becomes_none(self) -> None:
        # 模型给 -5 → 归一为 None；默认关闭兜底 → 保持 None
        plan = _detailPlan(rowLimit=-5)  # type: ignore[arg-type]
        result = _applyScopeRowLimit(plan, "列出所有收货记录")
        # _coerceRowLimit(-5) → None；默认 NL2SQL_NO_SCOPE_ROW_LIMIT=0 → 不注入兜底
        assert result.rowLimit is None

    def test_dirty_object_row_limit_normalized_to_none(self) -> None:
        # 模型给 {"a": 1} 对象 → 归一为 None
        plan = _detailPlan(rowLimit={"a": 1})  # type: ignore[arg-type]
        result = _applyScopeRowLimit(plan, "2025年的收货明细")
        # 归一为 None + 规则 2（时间范围）→ 保持 None
        assert result.rowLimit is None
