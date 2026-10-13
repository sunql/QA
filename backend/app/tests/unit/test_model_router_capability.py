"""model_router 按 supports_stream / supports_tools 过滤。

RED: selectModel 无能力过滤。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.model_router_service import (
    ModelCapability,
    ModelRouterService,
    RoutingContext,
)


@dataclass
class FakeConfig:
    id: int
    name: str
    is_active: bool = True
    cost_per_1k: Decimal = Decimal("0.001")
    cost_per_1k_input: Decimal = Decimal("0.001")
    model_name: str = "fake"
    provider: str = "openai"
    cost_threshold: Decimal = Decimal("999")
    weight: int = 10
    capability: ModelCapability | None = None


def test_tools_required_filters_out_no_tools_models():
    """需要 tools → 过滤掉 supports_tools=False 的模型。"""
    configs = [
        FakeConfig(id=1, name="gpt4", capability=ModelCapability(supports_tools=True)),
        FakeConfig(id=2, name="ollama-base", capability=ModelCapability(supports_tools=False)),
    ]
    router = ModelRouterService()
    selected = router.selectModel(
        configs=configs,
        prompt="use supplier_risk_agent",
        ctx=RoutingContext(sessionId="s1"),
        required_capabilities={"supports_tools"},
    )
    assert selected.id == 1


def test_stream_required_filters_out_no_stream_models():
    configs = [
        FakeConfig(id=1, name="gpt4", capability=ModelCapability(supports_stream=True)),
        FakeConfig(id=2, name="batch-only", capability=ModelCapability(supports_stream=False)),
    ]
    router = ModelRouterService()
    selected = router.selectModel(
        configs=configs,
        prompt="stream query",
        ctx=RoutingContext(sessionId="s1"),
        required_capabilities={"supports_stream"},
    )
    assert selected.id == 1


def test_no_capability_filter_returns_some_model():
    """不传 required_capabilities → 走原路由逻辑（成本 + 亲和 + 权重）。"""
    configs = [
        FakeConfig(id=1, name="expensive", cost_per_1k=Decimal("0.01")),
        FakeConfig(id=2, name="cheap", cost_per_1k=Decimal("0.001")),
    ]
    router = ModelRouterService()
    selected = router.selectModel(
        configs=configs,
        prompt="simple query",
        ctx=RoutingContext(sessionId="s1"),
    )
    assert selected is not None
    assert selected.id in (1, 2)


def test_capability_filter_no_match_raises():
    """required_capabilities 全部不满足 → 抛 NoAvailableModelError。"""
    from app.services.model_router_service import NoAvailableModelError
    configs = [
        FakeConfig(id=1, name="no-stream", capability=ModelCapability(supports_stream=False)),
    ]
    router = ModelRouterService()
    with pytest.raises(NoAvailableModelError):
        router.selectModel(
            configs=configs,
            prompt="stream needed",
            ctx=RoutingContext(sessionId="s1"),
            required_capabilities={"supports_stream"},
        )
