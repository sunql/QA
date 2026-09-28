import pytest
from pydantic import ValidationError
from app.domain.schemas_dq_rule_params import (
    RuleParams, _RangeParams, _InSetParams, _CompareParams,
    _RefParams, _CrossColumnParams, _RegexParams, _NotNullParams, _UniqueParams,
)


class TestNotNull:
    def test_accepts_empty(self):
        p = RuleParams.model_validate({"kind": "not_null"})
        assert isinstance(p, _NotNullParams)


class TestUnique:
    def test_accepts_empty(self):
        p = RuleParams.model_validate({"kind": "unique"})
        assert isinstance(p, _UniqueParams)


class TestRange:
    def test_min_only(self):
        p = RuleParams.model_validate({"kind": "range", "min": 0})
        assert p.min == 0 and p.max is None

    def test_max_only(self):
        p = RuleParams.model_validate({"kind": "range", "max": 100})

    def test_both(self):
        p = RuleParams.model_validate({"kind": "range", "min": 0, "max": 100})

    def test_neither_raises(self):
        with pytest.raises(ValidationError) as exc:
            RuleParams.model_validate({"kind": "range"})
        assert "min" in str(exc.value) or "max" in str(exc.value)

    def test_non_decimal_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "range", "min": "abc"})


class TestInSet:
    def test_values(self):
        p = RuleParams.model_validate({"kind": "in_set", "values": ["A", "B"]})
        assert p.values == ["A", "B"]

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": []})

    def test_quote_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": ["A'B"]})

    def test_control_char_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": ["A\x00B"]})

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": ["x" * 51]})


class TestRegex:
    def test_pattern(self):
        p = RuleParams.model_validate({"kind": "regex", "pattern": "^[0-9]+$"})
        assert p.pattern == "^[0-9]+$"

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "regex", "pattern": ""})

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "regex", "pattern": "x" * 501})

    def test_invalid_regex_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "regex", "pattern": "[unclosed"})


class TestCompare:
    def test_gt(self):
        p = RuleParams.model_validate({"kind": "compare", "op": ">", "value": 0})
        assert p.op == ">"

    def test_invalid_op_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "compare", "op": "~~", "value": 0})


class TestRef:
    def test_valid(self):
        p = RuleParams.model_validate({
            "kind": "ref", "ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY",
        })

    def test_invalid_identifier_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({
                "kind": "ref", "ref_table": "1SUPPLIER", "ref_column": "X",
            })


class TestCrossColumn:
    def test_basic(self):
        p = RuleParams.model_validate({
            "kind": "cross_column", "left": "RECEIVED_QTY", "op": "<=",
            "right": "ORDER_QTY", "factor": 1.05,
        })

    def test_factor_default(self):
        p = RuleParams.model_validate({
            "kind": "cross_column", "left": "A", "op": "=", "right": "B",
        })
        assert p.factor is None

    def test_factor_zero_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({
                "kind": "cross_column", "left": "A", "op": "=", "right": "B", "factor": 0,
            })


class TestDiscriminator:
    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "unknown"})
