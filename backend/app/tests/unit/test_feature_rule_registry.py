"""FeatureRuleRegistry 单元测试。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.feature_rule_registry import (
    FeatureRuleRegistry,
    feature_rule_registry,
)


@pytest.fixture
def freshRegistry() -> FeatureRuleRegistry:
    return FeatureRuleRegistry()


def test_get_rules_unwarmed_raises(freshRegistry: FeatureRuleRegistry) -> None:
    with pytest.raises(RuntimeError, match="未 warmUp"):
        freshRegistry.getEnabledRules("SUPPLIER", "FEATURE", "RISK")


async def test_warm_up_populates_index(freshRegistry: FeatureRuleRegistry) -> None:
    rule_a = SimpleNamespace(id=1, code="r1", data_object="SUPPLIER", data_layer="FEATURE",
                              target_level="RISK", feature_name="F1", enabled=True, priority=100, thresholds=[])
    rule_b = SimpleNamespace(id=2, code="r2", data_object="SUPPLIER", data_layer="FEATURE",
                              target_level="RISK", feature_name="F2", enabled=False, priority=100, thresholds=[])
    rule_c = SimpleNamespace(id=3, code="r3", data_object="SUPPLIER", data_layer="FEATURE",
                              target_level="QUALITY_SCORE", feature_name="F3", enabled=True, priority=100, thresholds=[])
    session = AsyncMock()
    session.execute.side_effect = [
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[rule_a, rule_b, rule_c])))),
        MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
    ]
    await freshRegistry.warmUp(session)

    risk = freshRegistry.getEnabledRules("SUPPLIER", "FEATURE", "RISK")
    assert {r.code for r in risk} == {"r1"}  # r2 disabled excluded
    quality = freshRegistry.getEnabledRules("SUPPLIER", "FEATURE", "QUALITY_SCORE")
    assert {r.code for r in quality} == {"r3"}


def test_invalidate_unwarmed_is_noop(freshRegistry: FeatureRuleRegistry) -> None:
    freshRegistry.invalidate()  # 不抛


async def test_reload_one_rule_not_found_clears_index() -> None:
    reg = FeatureRuleRegistry()
    reg._loaded = True  # 手动标记 warmed
    reg._rules = {("SUPPLIER", "FEATURE", "RISK"): ["placeholder"]}
    session = AsyncMock()
    session.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    await reg.reloadOne(session, 999)
    assert ("SUPPLIER", "FEATURE", "RISK") not in reg._rules


def test_module_singleton_exists() -> None:
    assert isinstance(feature_rule_registry, FeatureRuleRegistry)
