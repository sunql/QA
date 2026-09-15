"""规则结构化参数编译器（feat-dq-rule-params v1）。

纯函数无 IO；写入时调用，产物过 validate_expression 白名单。
evaluator 永远只读 data_quality_rule.rule_expression（无论结构化还是自定义），
本模块不参与评估期。

分发表放在 _dispatch() 函数里（非模块级 dict），Task 4-5 追加新 kind 时
在返回值里补条目即可，避免跨任务原地修改共享可变状态。
"""
from __future__ import annotations

from typing import Any, Callable

from app.domain.enums import RuleType
from app.domain.models import DataQualityRule
from app.domain.schemas_dq_rule_params import RuleParams
from app.services.data_quality_evaluators._common import (
    quote_identifier,
    validate_identifier,
)


def _quote(rule: DataQualityRule, name: str) -> str:
    """编译期引号助手：先强校验标识符，再按 PG 双引号风格引用。

    quote_identifier 对 adapter=None 走 PG 双引号默认；Task 4 起按需接收真实
    adapter 做方言感知引号（VALIDITY 等跨库 kind）。
    rule 参数为 Task 4 方言感知留位，当前未使用。
    """
    validate_identifier(name, role="column")
    return quote_identifier(None, name)


def _compileNotNull(rule: DataQualityRule, params: dict) -> str:
    col = _quote(rule, rule.target_column or "")
    return f"{col} IS NOT NULL"


def _compileUnique(rule: DataQualityRule, params: dict) -> str:
    col = _quote(rule, rule.target_column or "")
    return f"UNIQUE({col})"


def _dispatch() -> dict[RuleType, dict[str, Callable[[DataQualityRule, dict], str]]]:
    """(rule_type, kind) -> 编译函数 路由表。Task 4-5 在此追加新 kind。"""
    return {
        RuleType.COMPLETENESS: {"not_null": _compileNotNull},
        RuleType.UNIQUENESS: {"unique": _compileUnique},
    }


def compileRuleParams(rule: DataQualityRule, params: dict[str, Any]) -> str:
    """按 (rule_type, kind) 路由到 _compile<Kind>；params 先过 RuleParams 校验。"""
    ruleType = RuleType(rule.rule_type)
    validated = RuleParams.model_validate(params)
    fn = _dispatch().get(ruleType, {}).get(validated.kind)
    if fn is None:
        raise ValueError(
            f"rule_type={ruleType.value} 不支持 kind={validated.kind}（结构化参数）"
        )
    # params 为已验证的原始 dict；Task 4 起各 kind 编译函数需访问 typed 字段时改用 validated
    return fn(rule, params)


__all__ = ["compileRuleParams"]
