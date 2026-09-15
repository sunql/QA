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


def _requireColumn(rule: DataQualityRule) -> str:
    """VALIDITY kinds 强依赖 target_column：非空校验后走 PG 双引号引用。"""
    if not rule.target_column:
        raise ValueError(f"rule_type={rule.rule_type} 需要 target_column")
    return _quote(rule, rule.target_column)


def _validateParams(kind: str, params: dict) -> Any:
    """params 先过 RuleParams 校验；编译函数只用校验后模型的 typed 字段。

    kind 由编译函数注入（调用方直传 params 时不一定带 kind，分发表已确保
    (rule_type, kind) 匹配）；params 里的同名字段不覆盖编译函数的 kind。
    """
    return RuleParams.model_validate({**params, "kind": kind})


def _compileRange(rule: DataQualityRule, params: dict) -> str:
    p = _validateParams("range", params)
    col = _requireColumn(rule)
    lo, hi = p.min, p.max
    if lo is not None and hi is not None:
        return f"{col} BETWEEN {lo} AND {hi}"
    if lo is not None:
        return f"{col} >= {lo}"
    return f"{col} <= {hi}"


def _compileInSet(rule: DataQualityRule, params: dict) -> str:
    p = _validateParams("in_set", params)
    col = _requireColumn(rule)
    quoted = ",".join(f"'{v}'" for v in p.values)
    return f"{col} IN ({quoted})"


def _compileRegex(rule: DataQualityRule, params: dict) -> str:
    p = _validateParams("regex", params)
    col = _requireColumn(rule)
    return f"{col} ~ '{p.pattern}'"


def _compileCompare(rule: DataQualityRule, params: dict) -> str:
    p = _validateParams("compare", params)
    col = _requireColumn(rule)
    return f"{col} {p.op} {p.value}"


def _dispatch() -> dict[RuleType, dict[str, Callable[[DataQualityRule, dict], str]]]:
    """(rule_type, kind) -> 编译函数 路由表。Task 5 在此追加 ref / cross_column。"""
    return {
        RuleType.COMPLETENESS: {"not_null": _compileNotNull},
        RuleType.UNIQUENESS: {"unique": _compileUnique},
        RuleType.VALIDITY: {
            "range": _compileRange,
            "in_set": _compileInSet,
            "regex": _compileRegex,
            "compare": _compileCompare,
        },
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
    # 各编译函数内部以注入 kind 的方式再过一次 RuleParams 校验，拿 typed 字段；
    # 两次校验成本可忽略（写路径调用，非评估期热路径）。
    return fn(rule, params)


__all__ = ["compileRuleParams"]
