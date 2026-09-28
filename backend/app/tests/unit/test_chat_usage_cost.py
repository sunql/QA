"""ChatService._costFor 单元测试（feat-token-cache，2026-09-28）。

DeepSeek prompt cache：服务端基于 prefix matching 命中，usage.cached_tokens
非零时按差额计费（命中部分不计 prompt input 成本）。

本测试固定 _costFor 行为：
- cachedTokens=None / 0 → 全额按 prompt 计
- cachedTokens>0 → billable = max(0, prompt - cached)
- 边界 cached >= prompt → billable = 0
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.chat_usage import UsageMixin as ChatUsageMixin


@dataclass(frozen=True)
class _Cfg:
    cost_per_1k_input: float = 0.0014   # DeepSeek 实价
    cost_per_1k_output: float = 0.0028


def test_costFor_full_prompt_when_cached_tokens_none() -> None:
    """无 cachedTokens（缓存未命中或字段缺失）→ 按全额 prompt 计。"""
    cost = ChatUsageMixin._costFor(_Cfg(), promptTokens=1000, completionTokens=100)
    expected = Decimal(1000) * Decimal("0.0014") / 1000 + Decimal(100) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_zero_when_cached_tokens_absent_arg() -> None:
    """显式 cachedTokens=0 → 同 None（全额计）。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 100, cachedTokens=0)
    expected = Decimal(1000) * Decimal("0.0014") / 1000 + Decimal(100) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_reduces_input_when_cached_tokens_positive() -> None:
    """cachedTokens > 0 → 仅差额计入 input 成本。"""
    # prompt=1000, cached=800 → billable input=200
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 100, cachedTokens=800)
    expected = (
        Decimal(200) * Decimal("0.0014") / 1000
        + Decimal(100) * Decimal("0.0028") / 1000
    )
    assert cost == expected


def test_costFor_zero_input_cost_when_cached_equals_prompt() -> None:
    """cached >= prompt → billable input = 0（output 仍计）。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 50, cachedTokens=1000)
    expected = Decimal(50) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_clamps_billable_at_zero_when_cached_exceeds_prompt() -> None:
    """cached > prompt（异常：服务端不应返回但要稳）→ billable = 0。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 800, 50, cachedTokens=1000)
    expected = Decimal(50) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_multiplier_zero_matches_old_free_cache_behavior() -> None:
    """cacheHitMultiplier=0（默认）→ 命中按 0 计，与初版差额计费等价。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 100, cachedTokens=800, cacheHitMultiplier=0.0)
    # billable = (1000 - 800) + 800 * 0 = 200；按 input 价计
    expected = Decimal(200) * Decimal("0.0014") / 1000 + Decimal(100) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_multiplier_quarter_matches_deepseek_current_price() -> None:
    """cacheHitMultiplier=0.25（DeepSeek 当前价 miss 的 1/4）→ 命中按 1/4 计。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 100, cachedTokens=800, cacheHitMultiplier=0.25)
    # billable = (1000 - 800) + 800 * 0.25 = 400
    expected = Decimal(400) * Decimal("0.0014") / 1000 + Decimal(100) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_multiplier_full_charges_full_prompt() -> None:
    """cacheHitMultiplier=1.0 → 即使命中也按 miss 全额计（无折扣）。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 100, cachedTokens=800, cacheHitMultiplier=1.0)
    expected = Decimal(1000) * Decimal("0.0014") / 1000 + Decimal(100) * Decimal("0.0028") / 1000
    assert cost == expected


def test_costFor_multiplier_zero_when_no_cached() -> None:
    """无 cachedTokens 时 multiplier 参数无影响（兜底）。"""
    cost = ChatUsageMixin._costFor(_Cfg(), 1000, 100, cachedTokens=None, cacheHitMultiplier=0.25)
    expected = Decimal(1000) * Decimal("0.0014") / 1000 + Decimal(100) * Decimal("0.0028") / 1000
    assert cost == expected


# ============================================================================
# 4-2（feat-token-cache，2026-09-28 续）：chart / answer / wasted 路径的
# cachedTokens 透传到 _costFor（4 个 MEDIUM 的代码审查修复）。
# 此前 _summarizeUsage 对 chart/answer/wasted 的 _costFor 调用均不带
# cachedTokens → 这些阶段（占 token ~5%）的 cache 命中部分被按全额计费。
# 本批把 cachedTokens 串到 chart + answer + record 三条路径；wasted
# 路径无成功响应（Nl2SqlError 携带累计 token，不带 cachedTokens），
# 保持 cachedTokens=None（现有行为不变）。
# ============================================================================


def _makeOutcome(
    *,
    sqlConfig_cost: float = 0.0014,
    promptTokens: int = 1000,
    completionTokens: int = 100,
    wasted: tuple[int, int] = (0, 0),
    cachedTokens: int | None = None,
):
    """构造 _SqlOutcome 替身（frozen dataclass 字段多，直接构造麻烦）。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        sqlConfig=SimpleNamespace(
            id=1, model_name="deepseek-chat",
            cost_per_1k_input=sqlConfig_cost, cost_per_1k_output=0.0028,
        ),
        promptTokens=promptTokens,
        completionTokens=completionTokens,
        wasted=wasted,
        cachedTokens=cachedTokens,
    )


def _makeResp(*, promptTokens: int, completionTokens: int, cachedTokens: int | None = None):
    from types import SimpleNamespace
    return SimpleNamespace(
        promptTokens=promptTokens,
        completionTokens=completionTokens,
        cachedTokens=cachedTokens,
        modelName="deepseek-chat",
        content="",
    )


def _makeAnswerConfig():
    from types import SimpleNamespace
    return SimpleNamespace(
        id=2, model_name="deepseek-chat",
        cost_per_1k_input=0.0014, cost_per_1k_output=0.0028,
    )


class _UsageHost(ChatUsageMixin):
    """最小宿主类：让 UsageMixin 的方法（包括 instance method）能在 self 上调用。

    _summarizeUsage 内部走 self._costFor / self._costForSql —— 把它们都暴露成
    ChatUsageMixin 上的对应函数即可，无需任何依赖。
    """

    def __init__(self) -> None:  # noqa: D401 - 测试替身
        pass


def _makeUsageMixinSelf() -> _UsageHost:
    return _UsageHost()


def test_summarizeUsage_passes_chart_cached_tokens_to_costFor() -> None:
    """chart 阶段的 cachedTokens 必须透传到 _costFor，否则 cache 命中部分按全额计。

    chart prompt=500, cached=300, multiplier=0.25：
    billable_input = (500-300) + 300*0.25 = 275
    cost_input = 275 * 0.0014 / 1000 = 0.000385
    """
    outcome = _makeOutcome(promptTokens=1000, completionTokens=100)
    primary = outcome.sqlConfig  # type: ignore[assignment]
    answerResp = _makeResp(promptTokens=200, completionTokens=50)
    answerConfig = _makeAnswerConfig()
    wastedAnswer = (0, 0)
    self = _makeUsageMixinSelf()

    total, cost = ChatUsageMixin._summarizeUsage(
        self,
        outcome=outcome,
        chartPt=500, chartCt=10, chartCached=300,
        answerResp=answerResp, answerConfig=answerConfig,
        wastedAnswer=wastedAnswer,
        primary=primary,
        cacheHitMultiplier=0.25,
    )

    # chart 部分：billable=275
    chart_input = Decimal(275) * Decimal("0.0014") / Decimal(1000)
    chart_output = Decimal(10) * Decimal("0.0028") / Decimal(1000)
    expected_chart = chart_input + chart_output
    # SQL 部分（cachedTokens=None）按全额计：1000 + 100
    sql_input = Decimal(1000) * Decimal("0.0014") / Decimal(1000)
    sql_output = Decimal(100) * Decimal("0.0028") / Decimal(1000)
    expected_sql = sql_input + sql_output
    # Answer 部分（cachedTokens=None）按全额计：200 + 50
    ans_input = Decimal(200) * Decimal("0.0014") / Decimal(1000)
    ans_output = Decimal(50) * Decimal("0.0028") / Decimal(1000)
    expected_ans = ans_input + ans_output
    expected_cost = expected_sql + expected_chart + expected_ans
    assert cost == expected_cost, f"chart cachedTokens 没透传: got {cost} expected {expected_cost}"
    assert total == 1000 + 100 + 500 + 10 + 200 + 50


def test_summarizeUsage_passes_answer_cached_tokens_to_costFor() -> None:
    """answer 阶段的 cachedTokens 必须透传到 _costFor。

    answer prompt=800, cached=600, multiplier=0.25：
    billable_input = (800-600) + 600*0.25 = 350
    """
    outcome = _makeOutcome(promptTokens=1000, completionTokens=100)
    primary = outcome.sqlConfig  # type: ignore[assignment]
    answerResp = _makeResp(promptTokens=800, completionTokens=50, cachedTokens=600)
    answerConfig = _makeAnswerConfig()
    wastedAnswer = (0, 0)
    self = _makeUsageMixinSelf()

    total, cost = ChatUsageMixin._summarizeUsage(
        self,
        outcome=outcome,
        chartPt=500, chartCt=10,
        answerResp=answerResp, answerConfig=answerConfig,
        wastedAnswer=wastedAnswer,
        primary=primary,
        cacheHitMultiplier=0.25,
    )

    # Answer 部分（cachedTokens=600）按差额 + multiplier 计：billable=350
    # 全额对照版本（cachedTokens=None）：billable=800
    # 期望节省 = (800 - 350) × 0.0014/1000 = 600 × 0.75 × 0.0014/1000 = 0.00063
    expected_savings = Decimal(600) * Decimal("0.75") * Decimal("0.0014") / Decimal(1000)
    # 用答 cachedTokens=None 跑一次算出 baseline，再求差值
    outcome_full = _makeOutcome(promptTokens=1000, completionTokens=100)
    _, cost_full = ChatUsageMixin._summarizeUsage(
        self,
        outcome=outcome_full,
        chartPt=500, chartCt=10,
        answerResp=_makeResp(promptTokens=800, completionTokens=50, cachedTokens=None),
        answerConfig=answerConfig,
        wastedAnswer=(0, 0),
        primary=outcome_full.sqlConfig,  # type: ignore[arg-type]
        cacheHitMultiplier=0.25,
    )
    assert cost_full - cost == expected_savings, (
        f"answer cachedTokens 没透传：差额 {cost_full - cost} "
        f"≠ 期望节省 {expected_savings}"
    )


def test_summarizeUsage_chart_cached_tokens_none_no_discount() -> None:
    """chart cachedTokens=None → 按全额计（兜底；兼容旧调用方）。"""
    outcome = _makeOutcome(promptTokens=1000, completionTokens=100)
    primary = outcome.sqlConfig  # type: ignore[assignment]
    answerResp = _makeResp(promptTokens=200, completionTokens=50)
    answerConfig = _makeAnswerConfig()
    self = _makeUsageMixinSelf()

    total, cost = ChatUsageMixin._summarizeUsage(
        self,
        outcome=outcome,
        chartPt=500, chartCt=10, chartCached=None,
        answerResp=answerResp, answerConfig=answerConfig,
        wastedAnswer=(0, 0),
        primary=primary,
        cacheHitMultiplier=0.25,
    )

    # chart 按全额：500 + 10
    chart_full = Decimal(500) * Decimal("0.0014") / Decimal(1000) + Decimal(10) * Decimal("0.0028") / Decimal(1000)
    # 应大于带 cachedTokens 的版本
    outcome_with_cache = _makeOutcome(promptTokens=1000, completionTokens=100)
    _, cost_with_cache = ChatUsageMixin._summarizeUsage(
        self,
        outcome=outcome_with_cache,
        chartPt=500, chartCt=10, chartCached=300,
        answerResp=answerResp, answerConfig=answerConfig,
        wastedAnswer=(0, 0),
        primary=primary,
        cacheHitMultiplier=0.25,
    )
    assert cost > cost_with_cache, "chart cachedTokens=None 应按全额，cost 应更大"