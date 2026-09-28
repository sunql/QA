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