"""Model Router 路由服务单元测试。

TDD RED：定义加权随机、成本阈值、会话亲和、预算降级行为。
通过注入 FakeRng 实现确定性测试。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import getSettings
from app.domain.enums import ProviderType
from app.domain.exceptions import NoAvailableModelError
from app.services.model_router_service import ModelRouterService, RoutingContext
from app.services.token_counter_provider import providerTokenCounter


class FakeRng:
    """可控随机数发生器，返回固定值。"""

    def __init__(self, value: float) -> None:
        assert 0.0 <= value < 1.0
        self._value = value

    def random(self) -> float:
        return self._value


def makeConfig(
    *,
    id: int,
    name: str = "gpt-4o",
    provider: str = "openai",
    costInput: float = 0.03,
    costOutput: float = 0.06,
    threshold: float = 0.05,
    weight: int = 10,
    active: bool = True,
):
    from app.domain.models import LlmConfig

    return LlmConfig(
        id=id,
        model_name=name,
        provider=provider,
        api_endpoint=None,
        api_key_encrypted=None,
        cost_per_1k_input=Decimal(str(costInput)),
        cost_per_1k_output=Decimal(str(costOutput)),
        max_input_tokens=8000,
        weight=weight,
        cost_threshold=Decimal(str(threshold)),
        is_active=active,
    )


class TestModelRouterSelection:
    def test_raises_when_no_configs(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        with pytest.raises(NoAvailableModelError):
            router.selectModel([], "hi", RoutingContext(sessionId="s1"))

    def test_raises_when_all_disabled(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        configs = [makeConfig(id=1, active=False)]
        with pytest.raises(NoAvailableModelError):
            router.selectModel(configs, "hi", RoutingContext(sessionId="s1"))

    def test_returns_only_available_model(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        only = makeConfig(id=1)
        selected = router.selectModel([only], "Hello world", RoutingContext(sessionId="s1"))
        assert selected.id == 1

    def test_excludes_model_exceeding_threshold(self) -> None:
        # Arrange: A 单价极高，短 prompt 也会超阈值；B 正常
        router = ModelRouterService(rng=FakeRng(0.0))
        expensive = makeConfig(id=1, name="gpt-4o", costInput=10.0, threshold=0.01, weight=10)
        cheap = makeConfig(id=2, name="gpt-4o-mini", costInput=0.001, threshold=0.05, weight=10)
        prompt = "Hello world, this is a routing test prompt with some tokens."
        # Act
        selected = router.selectModel([expensive, cheap], prompt, RoutingContext(sessionId="s1"))
        # Assert: 仅 cheap 合格
        assert selected.id == 2

    def test_returns_cheapest_when_all_exceed_threshold(self) -> None:
        # Arrange: 两个模型都超阈值，应降级到最便宜
        router = ModelRouterService(rng=FakeRng(0.0))
        a = makeConfig(id=1, costInput=10.0, threshold=0.001, weight=10)
        b = makeConfig(id=2, costInput=5.0, threshold=0.001, weight=10)
        prompt = "A prompt long enough to exceed tiny thresholds with high unit price."
        # Act
        selected = router.selectModel([a, b], prompt, RoutingContext(sessionId="s1"))
        # Assert: b 更便宜
        assert selected.id == 2

    def test_forces_cheapest_when_session_budget_exceeded(self) -> None:
        # Arrange: 累计成本超过预算 -> 强制最便宜
        router = ModelRouterService(rng=FakeRng(0.0))
        expensive = makeConfig(id=1, costInput=0.03, threshold=1.0, weight=100)  # 高权重
        cheap = makeConfig(id=2, costInput=0.001, threshold=1.0, weight=1)
        ctx = RoutingContext(sessionId="s1", sessionCost=999.0, sessionTurnCount=0)
        # Act
        selected = router.selectModel([expensive, cheap], "hi", ctx)
        # Assert
        assert selected.id == 2


class TestSelectFallbackModel:
    """模型降级备选（5.4）。"""

    def test_selects_next_cheapest_excluding_failed_id(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        primary = makeConfig(id=1, costInput=0.03)
        cheapest = makeConfig(id=2, costInput=0.001)
        mid = makeConfig(id=3, costInput=0.01)
        fallback = router.selectFallbackModel([primary, cheapest, mid], excludeId=1)
        assert fallback.id == 2

    def test_excludes_disabled_candidates(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        primary = makeConfig(id=1, costInput=0.03)
        disabledCheap = makeConfig(id=2, costInput=0.001, active=False)
        enabledMid = makeConfig(id=3, costInput=0.01)
        fallback = router.selectFallbackModel([primary, disabledCheap, enabledMid], excludeId=1)
        assert fallback.id == 3

    def test_returns_none_when_only_failed_model_available(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        primary = makeConfig(id=1, costInput=0.03)
        assert router.selectFallbackModel([primary], excludeId=1) is None

    def test_returns_none_when_all_disabled(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        a = makeConfig(id=1, active=False)
        b = makeConfig(id=2, active=False)
        assert router.selectFallbackModel([a, b], excludeId=None) is None

    def test_does_not_mutate_inputs(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        primary = makeConfig(id=1, costInput=0.03)
        cheap = makeConfig(id=2, costInput=0.001)
        original = [primary, cheap]
        router.selectFallbackModel(original, excludeId=1)
        assert [c.id for c in original] == [1, 2]


class TestSessionAffinity:
    def test_returns_prior_model_within_affinity_window(self) -> None:
        # Arrange: 前 3 轮沿用 prior model
        router = ModelRouterService(rng=FakeRng(0.99))  # 即便随机偏向另一模型，亲和优先
        prior = makeConfig(id=1, name="gpt-4o", weight=1)
        other = makeConfig(id=2, name="gpt-4o-mini", weight=100)
        ctx = RoutingContext(sessionId="s1", sessionTurnCount=1, priorModelId=1)
        # Act
        selected = router.selectModel([prior, other], "hi", ctx)
        # Assert
        assert selected.id == 1

    def test_affinity_does_not_apply_after_window(self) -> None:
        # Arrange: 第 4 轮起不再强制亲和
        router = ModelRouterService(rng=FakeRng(0.99))  # 偏向 other
        prior = makeConfig(id=1, name="gpt-4o", weight=1)
        other = makeConfig(id=2, name="gpt-4o-mini", weight=100)
        ctx = RoutingContext(sessionId="s1", sessionTurnCount=3, priorModelId=1)
        # Act
        selected = router.selectModel([prior, other], "hi", ctx)
        # Assert: 不再亲和，加权随机选 other
        assert selected.id == 2

    def test_affinity_skips_disabled_prior(self) -> None:
        router = ModelRouterService(rng=FakeRng(0.0))
        prior = makeConfig(id=1, active=False, weight=100)
        other = makeConfig(id=2, weight=10)
        ctx = RoutingContext(sessionId="s1", sessionTurnCount=0, priorModelId=1)
        selected = router.selectModel([prior, other], "hi", ctx)
        assert selected.id == 2


class TestWeightedRandom:
    def test_weight_zero_never_selected_when_alternative_exists(self) -> None:
        # Arrange: A 权重 0，B 权重 1；多次抽样应总是 B
        router = ModelRouterService(rng=FakeRng(0.0))
        a = makeConfig(id=1, name="gpt-4o", weight=0, threshold=1.0)
        b = makeConfig(id=2, name="gpt-4o-mini", weight=1, threshold=1.0)
        # 多次（同 rng 值）都应选 B
        for _ in range(5):
            selected = router.selectModel([a, b], "hi", RoutingContext(sessionId="s1"))
            assert selected.id == 2

    def test_weighted_choice_picks_higher_weight_with_high_rng(self) -> None:
        # Arrange: A weight=1, B weight=99；rng=0.99 偏向尾部 -> B
        router = ModelRouterService(rng=FakeRng(0.99))
        a = makeConfig(id=1, name="gpt-4o", weight=1, threshold=1.0)
        b = makeConfig(id=2, name="gpt-4o-mini", weight=99, threshold=1.0)
        selected = router.selectModel([a, b], "hi", RoutingContext(sessionId="s1"))
        assert selected.id == 2


class TestProviderTokenCounterHelper:
    def test_provider_counter_returns_tiktoken_for_openai(self) -> None:
        from app.infrastructure.token_counter.tiktoken_counter import TiktokenCounter

        counter = providerTokenCounter(ProviderType.OPENAI)
        assert isinstance(counter, TiktokenCounter)
