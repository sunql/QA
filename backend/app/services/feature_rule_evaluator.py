"""Feature Rule 评估器（spec §6.2，纯同步，无 IO / 无 LLM）。

- 单条规则：按 severity 由重到轻检查阈值档，第一个匹配的档位命中（break）。
- 跨规则聚合：MAX severity（`min(_SEVERITY_ORDER[...])`，HIGH 最严重）。
- 输入取自 `feature_rule_registry` 内存索引，调用方需先 warmUp。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.domain.enums import RuleOperator, Severity
from app.services.feature_rule_registry import (
    FeatureRuleReady,
    FeatureThresholdReady,
    feature_rule_registry,
)

logger = logging.getLogger(__name__)

# Severity 排序：HIGH(0) 最严重 → INFO(3) 最轻；MAX severity 取序号最小者。
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.HIGH: 0,
    Severity.MEDIUM: 1,
    Severity.LOW: 2,
    Severity.INFO: 3,
}

_OPERATORS: dict[RuleOperator, Callable[[Decimal, Decimal], bool]] = {
    RuleOperator.LT: lambda value, threshold: value < threshold,
    RuleOperator.LTE: lambda value, threshold: value <= threshold,
    RuleOperator.GT: lambda value, threshold: value > threshold,
    RuleOperator.GTE: lambda value, threshold: value >= threshold,
    # LT_INVERSE：0-1 区间 RISK_SCORE（越低越差），比较语义同 LT，
    # 保留独立运算符以区分「反向分数」语义（前端展示 / LLM 抽取用）。
    RuleOperator.LT_INVERSE: lambda value, threshold: value < threshold,
}

_SOURCE_NO_RULES = "no_rules"
_SOURCE_NO_MATCH = "no_match"


@dataclass(frozen=True)
class RuleHit:
    """单条规则命中的阈值档快照。"""

    rule_code: str
    feature_name: str
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    actual_value: Decimal


@dataclass(frozen=True)
class RuleEvaluation:
    """一次评估的聚合结果。

    matched_severity_source：`rule:<code>`（命中）/ `no_rules` / `no_match`。
    """

    matched_severity: Severity | None
    matched_severity_source: str
    contributing_rules: tuple[RuleHit, ...]


def _coerceTier(
    rule_code: str, tier: FeatureThresholdReady
) -> tuple[Severity, RuleOperator] | None:
    """把 DB 字符串档位收敛成枚举；非法值跳过该档并告警（不静默吞掉）。"""
    try:
        return Severity(tier.severity), RuleOperator(tier.operator)
    except ValueError:
        logger.warning(
            "跳过非法阈值档位 rule=%s severity=%s operator=%s",
            rule_code, tier.severity, tier.operator,
        )
        return None


def _evaluateRule(rule: FeatureRuleReady, value: Decimal) -> RuleHit | None:
    """按 severity 由重到轻检查档位，返回第一个命中；无命中返回 None。"""
    tiers: list[tuple[Severity, RuleOperator, FeatureThresholdReady]] = []
    for tier in rule.thresholds:
        coerced = _coerceTier(rule.code, tier)
        if coerced is None:
            continue
        tiers.append((coerced[0], coerced[1], tier))

    for severity, operator, tier in sorted(tiers, key=lambda t: _SEVERITY_ORDER[t[0]]):
        if _OPERATORS[operator](value, tier.threshold_value):
            return RuleHit(
                rule_code=rule.code,
                feature_name=rule.feature_name,
                severity=severity,
                operator=operator,
                threshold_value=tier.threshold_value,
                actual_value=value,
            )
    return None


class FeatureRuleEvaluator:
    """无状态评估器：作用域 → 规则 → MAX severity。"""

    @staticmethod
    def evaluate(
        data_object: str,
        data_layer: str,
        target_level: str,
        feature_values: Mapping[str, Decimal | None],
    ) -> RuleEvaluation:
        rules = feature_rule_registry.getEnabledRules(
            data_object, data_layer, target_level
        )
        if not rules:
            return RuleEvaluation(None, _SOURCE_NO_RULES, ())

        hits: list[RuleHit] = []
        for rule in rules:
            value = feature_values.get(rule.feature_name)
            if value is None:
                continue  # 特征缺失：跳过该规则，不视为命中
            hit = _evaluateRule(rule, value)
            if hit is not None:
                hits.append(hit)

        if not hits:
            return RuleEvaluation(None, _SOURCE_NO_MATCH, ())
        worst = min(hits, key=lambda hit: _SEVERITY_ORDER[hit.severity])
        return RuleEvaluation(worst.severity, f"rule:{worst.rule_code}", tuple(hits))
