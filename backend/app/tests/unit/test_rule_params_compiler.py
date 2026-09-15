"""rule_params_compiler 单元测试（feat-dq-rule-params Task 3）。

只覆盖分发器 + not_null / unique 两个 kind；其余 6 种由 Task 4-5 补。
"""
import pytest

from app.domain.enums import RuleType
from app.domain.exceptions import ValidationError
from app.domain.models import DataQualityRule
from pydantic import ValidationError as PydanticValidationError
from app.services.data_quality_evaluators._common import validate_expression
from app.services.data_quality_evaluators.rule_params_compiler import (
    _compileCompare,
    _compileCrossColumn,
    _compileInSet,
    _compileNotNull,
    _compileRange,
    _compileRef,
    _compileRegex,
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


class TestCompileRange:
    def test_min_max(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        assert _compileRange(rule, {"min": 0, "max": 100}) == '"ORDER_QTY" BETWEEN 0 AND 100'

    def test_min_only(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        assert _compileRange(rule, {"min": 0}) == '"ORDER_QTY" >= 0'

    def test_max_only(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        assert _compileRange(rule, {"max": 100}) == '"ORDER_QTY" <= 100'

    def test_passes_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        validate_expression(_compileRange(rule, {"min": 0, "max": 100}))

    def test_missing_column_raises(self):
        rule = _rule(RuleType.VALIDITY, "")
        rule.target_column = None
        with pytest.raises(ValueError):
            _compileRange(rule, {"min": 0})


class TestCompileInSet:
    def test_basic(self):
        rule = _rule(RuleType.VALIDITY, "STATUS")
        out = _compileInSet(rule, {"values": ["A", "B"]})
        assert out == '"STATUS" IN (\'A\',\'B\')'

    def test_passes_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "STATUS")
        validate_expression(_compileInSet(rule, {"values": ["A"]}))

    def test_empty_values_raises(self):
        rule = _rule(RuleType.VALIDITY, "STATUS")
        with pytest.raises(PydanticValidationError):
            _compileInSet(rule, {"values": []})


class TestCompileRegex:
    def test_basic(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_NO")
        out = _compileRegex(rule, {"pattern": "^[A-Z0-9]+$"})
        assert out == "\"ORDER_NO\" ~ '^[A-Z0-9]+$'"

    def test_passes_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_NO")
        validate_expression(_compileRegex(rule, {"pattern": "^[0-9]+$"}))

    def test_invalid_pattern_raises(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_NO")
        with pytest.raises(PydanticValidationError):
            _compileRegex(rule, {"pattern": "(["})


class TestCompileCompare:
    def test_gt(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        assert _compileCompare(rule, {"op": ">", "value": 0}) == '"PRICE" > 0'

    def test_ne(self):
        rule = _rule(RuleType.VALIDITY, "FLAG")
        assert _compileCompare(rule, {"op": "!=", "value": 0}) == '"FLAG" != 0'

    def test_passes_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        validate_expression(_compileCompare(rule, {"op": ">", "value": 0}))

    def test_illegal_op_raises(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        with pytest.raises(PydanticValidationError):
            _compileCompare(rule, {"op": "LIKE", "value": 0})


class TestDispatcherValidity:
    def test_validity_routes(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        assert "BETWEEN" in compileRuleParams(rule, {"kind": "range", "min": 0, "max": 100})
        assert "IN (" in compileRuleParams(rule, {"kind": "in_set", "values": ["A"]})
        assert "~" in compileRuleParams(rule, {"kind": "regex", "pattern": "^x$"})
        assert "> 0" in compileRuleParams(rule, {"kind": "compare", "op": ">", "value": 0})


class TestCompileRef:
    def test_basic(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        out = _compileRef(rule, {"ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"})
        assert out == "REF SUPPLIER.SUPPLIER_KEY"

    def test_whitelist(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        validate_expression(_compileRef(rule, {"ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"}))

    def test_invalid_identifier_raises(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        with pytest.raises(Exception):
            _compileRef(rule, {"ref_table": "1BAD", "ref_column": "X"})


class TestCompileCrossColumn:
    def test_no_factor(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        out = _compileCrossColumn(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY",
        })
        assert out == '"RECEIVED_QTY" <= "ORDER_QTY"'

    def test_with_factor(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        out = _compileCrossColumn(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY", "factor": 1.05,
        })
        assert out == '"RECEIVED_QTY" <= "ORDER_QTY" * 1.05'

    def test_whitelist(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        validate_expression(_compileCrossColumn(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY", "factor": 1.05,
        }))


class TestDispatcherFull:
    def test_referential(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        out = compileRuleParams(rule, {
            "kind": "ref", "ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY",
        })
        assert out.startswith("REF ")

    def test_consistency(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        out = compileRuleParams(rule, {
            "kind": "cross_column",
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY", "factor": 1.05,
        })
        assert "RECEIVED_QTY" in out and "ORDER_QTY" in out


@pytest.mark.parametrize("ruleType,params", [
    (RuleType.COMPLETENESS, {"kind": "not_null"}),
    (RuleType.UNIQUENESS, {"kind": "unique"}),
    (RuleType.VALIDITY, {"kind": "range", "min": 0, "max": 100}),
    (RuleType.VALIDITY, {"kind": "in_set", "values": ["A", "B"]}),
    (RuleType.VALIDITY, {"kind": "regex", "pattern": "^[0-9]+$"}),
    (RuleType.VALIDITY, {"kind": "compare", "op": ">", "value": 0}),
    (RuleType.REFERENTIAL, {"kind": "ref", "ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"}),
    (RuleType.CONSISTENCY, {"kind": "cross_column",
                            "left": "A", "op": "<=", "right": "B", "factor": 1.05}),
])
def test_all_kinds_pass_whitelist(ruleType, params):
    rule = _rule(ruleType, "PO_LINE_KEY")
    out = compileRuleParams(rule, params)
    validate_expression(out)

