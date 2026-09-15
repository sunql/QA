"""rule_params_compiler 单元测试（feat-dq-rule-params Task 3）。

只覆盖分发器 + not_null / unique 两个 kind；其余 6 种由 Task 4-5 补。
"""
import pytest

from app.domain.enums import RuleType
from app.domain.exceptions import ValidationError
from app.domain.models import DataQualityRule
from app.services.data_quality_evaluators._common import validate_expression
from app.services.data_quality_evaluators.rule_params_compiler import (
    _compileNotNull,
    _compileUnique,
    compileRuleParams,
)


def _rule(ruleType: RuleType, column: str = "PO_LINE_KEY") -> DataQualityRule:
    return DataQualityRule(
        id=1, rule_code="X", rule_name="X", rule_type=ruleType.value,
        target_table="PO_LINE", target_column=column,
    )


class TestCompileNotNull:
    def test_returns_is_not_null(self):
        rule = _rule(RuleType.COMPLETENESS, "ORDER_DATE")
        out = _compileNotNull(rule, {})
        assert out == '"ORDER_DATE" IS NOT NULL'

    def test_passes_whitelist(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        validate_expression(_compileNotNull(rule, {}))

    def test_invalid_column_raises(self):
        rule = _rule(RuleType.COMPLETENESS, "bad;col")
        with pytest.raises(ValidationError):
            _compileNotNull(rule, {})

    def test_missing_column_raises(self):
        rule = _rule(RuleType.COMPLETENESS, "")
        rule.target_column = None
        with pytest.raises(ValidationError):
            _compileNotNull(rule, {})


class TestCompileUnique:
    def test_returns_unique(self):
        rule = _rule(RuleType.UNIQUENESS, "PO_LINE_KEY")
        out = _compileUnique(rule, {})
        assert out == 'UNIQUE("PO_LINE_KEY")'

    def test_passes_whitelist(self):
        rule = _rule(RuleType.UNIQUENESS, "PO_LINE_KEY")
        validate_expression(_compileUnique(rule, {}))


class TestDispatcher:
    def test_completeness_routes_to_not_null(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        out = compileRuleParams(rule, {"kind": "not_null"})
        assert "IS NOT NULL" in out

    def test_uniqueness_routes_to_unique(self):
        rule = _rule(RuleType.UNIQUENESS, "PO_LINE_KEY")
        out = compileRuleParams(rule, {"kind": "unique"})
        assert "UNIQUE(" in out

    def test_unsupported_kind_raises(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        with pytest.raises(ValueError):
            compileRuleParams(rule, {"kind": "in_set"})

    def test_unknown_kind_raises(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        with pytest.raises(ValueError):
            compileRuleParams(rule, {"kind": "bogus"})

    def test_valid_kind_wrong_rule_type_raises_value_error(self):
        # unique 是合法 kind（过 RuleParams 校验），但 COMPLETENESS 分发表里没有，
        # 必须命中 dispatcher 自己的 raise ValueError 分支而非 pydantic ValidationError。
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        with pytest.raises(ValueError):
            compileRuleParams(rule, {"kind": "unique"})

    def test_missing_kind_raises(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        with pytest.raises(ValueError):
            compileRuleParams(rule, {})
