"""FeatureRuleEvaluator 单元测试（spec §6.2）。"""
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.domain.enums import RuleOperator, Severity
from app.services.feature_rule_evaluator import (
    FeatureRuleEvaluator,
    RuleEvaluation,
    RuleHit,
)
from app.services.feature_rule_registry import (
    FeatureRuleReady,
    FeatureThresholdReady,
    feature_rule_registry,
)

_SCOPE = ("SUPPLIER", "FEATURE", "RISK")


@pytest.fixture(autouse=True)
def _isolatedRegistry() -> Iterator[None]:
    """隔离模块 singleton：用例前置空索引并标记 warmed，用例后还原。"""
    original_rules = feature_rule_registry._rules
    original_loaded = feature_rule_registry._loaded
    feature_rule_registry._rules = {}
    feature_rule_registry._loaded = True
    yield
    feature_rule_registry._rules = original_rules
    feature_rule_registry._loaded = original_loaded


def _rule(
    code: str, feature: str, thresholds: list[FeatureThresholdReady]
) -> FeatureRuleReady:
    return FeatureRuleReady(
        id=hash(code) & 0xFFFF, code=code, data_object="SUPPLIER",
        data_layer="FEATURE", target_level="RISK", feature_name=feature,
        enabled=True, priority=100, thresholds=tuple(thresholds),
    )


def _seedRules(*rules: FeatureRuleReady) -> None:
    feature_rule_registry._rules[_SCOPE] = list(rules)


def _evaluate(feature_values: Mapping[str, Decimal | None]) -> RuleEvaluation:
    return FeatureRuleEvaluator.evaluate(*_SCOPE, feature_values)


def test_empty_rule_set_returns_no_rules() -> None:
    ev = _evaluate({})

    assert ev.matched_severity is None
    assert ev.matched_severity_source == "no_rules"
    assert ev.contributing_rules == ()


def test_lt_operator_triggers_when_value_below_threshold() -> None:
    _seedRules(_rule("otd", "OTD", [
        FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1),
    ]))

    ev = _evaluate({"OTD": Decimal(85)})

    assert ev.matched_severity.value == "HIGH"
    assert ev.matched_severity_source == "rule:otd"
    assert ev.contributing_rules == (
        RuleHit("otd", "OTD", Severity.HIGH, RuleOperator.LT, Decimal(90), Decimal(85)),
    )


def test_lt_operator_does_not_trigger_at_boundary() -> None:
    _seedRules(_rule("otd", "OTD", [
        FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1),
    ]))

    ev = _evaluate({"OTD": Decimal(90)})

    assert ev.matched_severity is None


@pytest.mark.parametrize(
    ("operator", "threshold", "hit_value", "miss_value"),
    [
        ("lt", Decimal(90), Decimal(89), Decimal(90)),
        ("lte", Decimal(90), Decimal(90), Decimal(91)),
        ("gt", Decimal(5), Decimal(6), Decimal(5)),
        ("gte", Decimal(5), Decimal(5), Decimal(4)),
        ("lt_inverse", Decimal("0.6"), Decimal("0.5"), Decimal("0.6")),
    ],
)
def test_operator_boundaries(
    operator: str, threshold: Decimal, hit_value: Decimal, miss_value: Decimal
) -> None:
    _seedRules(_rule("r", "F", [
        FeatureThresholdReady("MEDIUM", operator, threshold, None, 1),
    ]))

    assert _evaluate({"F": hit_value}).matched_severity == Severity.MEDIUM
    assert _evaluate({"F": miss_value}).matched_severity is None


def test_tier_severity_ordered_first_hit_wins() -> None:
    _seedRules(_rule("score", "RISK_SCORE", [
        FeatureThresholdReady("HIGH", "lt_inverse", Decimal("0.6"), None, 1),
        FeatureThresholdReady("MEDIUM", "lt_inverse", Decimal("0.8"), None, 1),
        FeatureThresholdReady("LOW", "lt_inverse", Decimal("1.01"), None, 1),
    ]))

    # 0.55 同时低于三档阈值 → 按 severity 顺序取最重的 HIGH，且只记一次命中。
    ev = _evaluate({"RISK_SCORE": Decimal("0.55")})

    assert ev.matched_severity.value == "HIGH"
    assert len(ev.contributing_rules) == 1


def test_cross_rule_max_severity() -> None:
    _seedRules(
        _rule("otd", "OTD", [FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1)]),
        _rule("defect", "DEFECT", [
            FeatureThresholdReady("MEDIUM", "gt", Decimal(5), "%", 1),
        ]),
    )

    # OTD 命中 HIGH，DEFECT 命中 MEDIUM → MAX severity = HIGH，两条都进 contributing。
    ev = _evaluate({"OTD": Decimal(85), "DEFECT": Decimal(7)})

    assert ev.matched_severity.value == "HIGH"
    assert ev.matched_severity_source == "rule:otd"
    assert [h.severity for h in ev.contributing_rules] == [Severity.HIGH, Severity.MEDIUM]


def test_missing_value_skips_rule() -> None:
    _seedRules(_rule("otd", "OTD", [
        FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1),
    ]))

    ev = _evaluate({"OTD": None})

    assert ev.matched_severity is None
    assert ev.matched_severity_source == "no_match"
    assert ev.contributing_rules == ()


def test_no_match_when_value_above_threshold() -> None:
    _seedRules(_rule("otd", "OTD", [
        FeatureThresholdReady("HIGH", "lt", Decimal(90), "%", 1),
    ]))

    ev = _evaluate({"OTD": Decimal(95)})

    assert ev.matched_severity is None
    assert ev.matched_severity_source == "no_match"


def test_evaluation_immutable() -> None:
    ev = RuleEvaluation(None, "no_rules", ())

    with pytest.raises(FrozenInstanceError):
        ev.matched_severity = Severity.HIGH  # type: ignore[misc]
