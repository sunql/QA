"""推导引擎单测（纯函数，无 IO）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.enums import DataType, DerivationType, RuleType, Severity
from app.domain.exceptions import ValidationError
from app.services.data_quality_evaluators._common import validate_expression
from app.services.data_quality_rule_generator import (
    BlockedProperty,
    ClassContext,
    ColumnMeta,
    JoinEdgeMeta,
    PropertyMeta,
    SchemaIndex,
    buildRuleCode,
    deriveSuggestions,
)

CTX = ClassContext(class_id=1, class_name="PurchaseOrder", source_table="PORDER", object_type="Transaction")
SCHEMA = SchemaIndex(tables={
    "PORDER": {
        "PO_KEY": ColumnMeta("PO_KEY", "varchar", nullable=False),
        "STATUS": ColumnMeta("STATUS", "varchar", nullable=True),
        "SUPPLIER_KEY": ColumnMeta("SUPPLIER_KEY", "varchar", nullable=True),
        "PO_DATE": ColumnMeta("PO_DATE", "varchar", nullable=True),
        "AMOUNT": ColumnMeta("AMOUNT", "varchar", nullable=True),
        "QTY": ColumnMeta("QTY", "varchar", nullable=True),
        "IS_OK": ColumnMeta("IS_OK", "varchar", nullable=True),
    }
})


def test_build_rule_code_deterministic():
    assert (
        buildRuleCode("PurchaseOrder", "po_key", RuleType.UNIQUENESS)
        == "DQ_PURCHASEORDER_PO_KEY_UNIQUENESS"
    )


def test_build_rule_code_truncates_with_hash_suffix():
    code = buildRuleCode("C" * 80, "P" * 60, RuleType.COMPLETENESS)
    assert len(code) <= 100 and code.startswith("DQ_")


def _prop(**kw):
    base = dict(property_id=10, property_name="po_key", source_column="PO_KEY",
                data_type="STRING", is_primary_key=True, is_foreign_key=False,
                ref_class=None, allowed_values=None)
    return PropertyMeta(**{**base, **kw})


def test_pk_yields_uniqueness_and_completeness():
    sugg, blocked = deriveSuggestions(CTX, [_prop()], [], SCHEMA)
    types = {s.rule_type for s in sugg}
    assert types == {RuleType.UNIQUENESS, RuleType.COMPLETENESS}
    assert all(s.confidence == "HIGH" for s in sugg)
    assert blocked == []


def test_fk_to_non_reference_class_yields_referential():
    ref = ClassContext(class_id=2, class_name="Supplier", source_table="BPSUPPLIER", object_type="Master")
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="supplier_key", source_column="SUPPLIER_KEY",
        is_primary_key=False, is_foreign_key=True, ref_class=ref,
        ref_key_column="SUPPLIER_KEY")], [], SCHEMA)
    ref_rules = [s for s in sugg if s.rule_type == RuleType.REFERENTIAL]
    assert len(ref_rules) == 1
    assert ref_rules[0].rule_expression == "REF BPSUPPLIER.SUPPLIER_KEY"
    assert ref_rules[0].derivation_type == DerivationType.FK_DERIVED


def test_ref_to_reference_class_yields_validity_dict_not_referential():
    ref = ClassContext(class_id=3, class_name="PoStatusDict", source_table="PO_STATUS", object_type="Reference")
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="status", source_column="STATUS",
        is_primary_key=False, is_foreign_key=True, ref_class=ref,
        ref_key_column="STATUS")], [], SCHEMA)
    validity = [s for s in sugg if s.rule_type == RuleType.VALIDITY]
    assert len(validity) == 1
    assert validity[0].rule_expression == (
        '"STATUS" IN (SELECT "STATUS" FROM "PO_STATUS")'
    )
    assert validity[0].derivation_type == DerivationType.DICT_REF
    assert not [s for s in sugg if s.rule_type == RuleType.REFERENTIAL]


def test_allowed_values_yields_validity():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="status", source_column="STATUS", is_primary_key=False,
        allowed_values=["NEW", "CONFIRMED"])], [], SCHEMA)
    v = [s for s in sugg if s.derivation_type == DerivationType.ALLOWED_VALUES]
    assert v[0].rule_expression == "STATUS IN ('NEW','CONFIRMED')"


def test_type_mismatch_yields_regex_validity():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="amount", source_column="AMOUNT", data_type="DECIMAL",
        is_primary_key=False)], [], SCHEMA)
    v = [s for s in sugg if s.derivation_type == DerivationType.NOT_NULL]  # 不该有
    assert v == []
    regex_rules = [s for s in sugg if "AMOUNT ~" in (s.rule_expression or "")]
    assert len(regex_rules) == 1


def test_join_edge_yields_consistency():
    edge = JoinEdgeMeta(target_table="PORDERQ", source_columns=["PO_KEY"],
                        target_columns=["PO_KEY"], target_date_columns=["RECEIPT_DATE"])
    sugg, _ = deriveSuggestions(
        CTX, [_prop(property_name="po_date", source_column="PO_DATE",
                    data_type="DATETIME", is_primary_key=False)], [edge], SCHEMA)
    c = [s for s in sugg if s.rule_type == RuleType.CONSISTENCY]
    assert len(c) == 1
    assert c[0].rule_expression == (
        'EXISTS (SELECT 1 FROM "PORDERQ" WHERE "PORDERQ"."PO_KEY" = "PORDER"."PO_KEY" '
        'AND "PORDERQ"."RECEIPT_DATE" >= "PORDER"."PO_DATE")'
    )


def test_missing_table_blocks_all():
    sugg, blocked = deriveSuggestions(CTX, [_prop()], [], None)
    assert sugg == []
    assert blocked[0].reason == "数据源 schema 未缓存"


def test_missing_column_blocks_property():
    sugg, blocked = deriveSuggestions(CTX, [_prop(source_column=None)], [], SCHEMA)
    assert sugg == []
    assert any(b.reason == "未配置物理列映射" for b in blocked)


def test_schema_missing_column_blocks_property():
    sugg, blocked = deriveSuggestions(
        CTX, [_prop(source_column="NOPE")], [], SCHEMA)
    assert sugg == []
    assert any("NOPE" in b.reason for b in blocked)


def test_allowed_value_with_quote_rejected():
    with pytest.raises(ValidationError):
        deriveSuggestions(CTX, [_prop(
            property_name="status", source_column="STATUS", is_primary_key=False,
            allowed_values=["OK'--"])], [], SCHEMA)


def test_non_text_column_skips_regex():
    numeric_schema = SchemaIndex(tables={
        "PORDER": {"AMOUNT": ColumnMeta("AMOUNT", "numeric", nullable=True)}
    })
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="amount", source_column="AMOUNT", data_type="DECIMAL",
        is_primary_key=False)], [], numeric_schema)
    assert not [s for s in sugg if "AMOUNT ~" in (s.rule_expression or "")]


def test_int_regex_validity():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="qty", source_column="QTY", data_type="INT",
        is_primary_key=False)], [], SCHEMA)
    regex = [s for s in sugg if s.rule_expression ==
             "QTY IS NULL OR QTY ~ '^-?[0-9]+$'"]
    assert len(regex) == 1
    assert regex[0].derivation_type == DerivationType.LLM_DERIVED


def test_boolean_flag_validity():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="is_ok", source_column="IS_OK", data_type="BOOLEAN",
        is_primary_key=False)], [], SCHEMA)
    expr = [s.rule_expression for s in sugg if s.rule_type == RuleType.VALIDITY]
    assert expr == ["IS_OK IS NULL OR IS_OK IN ('true','false','t','f','1','0')"]


def test_nullable_regex_has_optional_prefix():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="amount", source_column="AMOUNT", data_type="DECIMAL",
        is_primary_key=False)], [], SCHEMA)
    regex = [s for s in sugg if "AMOUNT ~" in (s.rule_expression or "")]
    assert regex[0].rule_expression.startswith("AMOUNT IS NULL OR AMOUNT ~")


def test_source_table_none_blocks_all():
    no_table_ctx = ClassContext(class_id=1, class_name="PurchaseOrder",
                                source_table=None, object_type="Transaction")
    sugg, blocked = deriveSuggestions(no_table_ctx, [_prop()], [], SCHEMA)
    assert sugg == []
    assert blocked == [BlockedProperty("po_key", "类未配置 source_table")]


def test_allowed_value_too_long_rejected():
    with pytest.raises(ValidationError):
        deriveSuggestions(CTX, [_prop(
            property_name="status", source_column="STATUS", is_primary_key=False,
            allowed_values=["X" * 51])], [], SCHEMA)


def test_fk_without_ref_class_raises():
    with pytest.raises(ValidationError):
        deriveSuggestions(CTX, [_prop(
            property_name="supplier_key", source_column="SUPPLIER_KEY",
            is_primary_key=False, is_foreign_key=True, ref_class=None)], [], SCHEMA)


def test_fk_without_ref_source_table_is_blocked():
    ref = ClassContext(class_id=2, class_name="Supplier", source_table=None,
                       object_type="Master")
    sugg, blocked = deriveSuggestions(CTX, [_prop(
        property_name="supplier_key", source_column="SUPPLIER_KEY",
        is_primary_key=False, is_foreign_key=True, ref_class=ref,
        ref_key_column="SUPPLIER_KEY")], [], SCHEMA)
    assert sugg == []
    assert blocked == [BlockedProperty("supplier_key", "引用类未配置 source_table")]


def test_join_edge_skips_non_datetime_property():
    edge = JoinEdgeMeta(target_table="PORDERQ", source_columns=["PO_KEY"],
                        target_columns=["PO_KEY"], target_date_columns=["RECEIPT_DATE"])
    sugg, _ = deriveSuggestions(
        CTX, [_prop(property_name="amount", source_column="AMOUNT",
                    data_type=DataType.DECIMAL.value, is_primary_key=False)],
        [edge], SCHEMA)
    assert not [s for s in sugg if s.rule_type == RuleType.CONSISTENCY]


def test_fk_without_ref_key_column_is_blocked():
    ref = ClassContext(class_id=2, class_name="Supplier", source_table="BPSUPPLIER",
                       object_type="Master")
    sugg, blocked = deriveSuggestions(CTX, [_prop(
        property_name="supplier_key", source_column="SUPPLIER_KEY",
        is_primary_key=False, is_foreign_key=True, ref_class=ref,
        ref_key_column=None)], [], SCHEMA)
    assert sugg == []
    assert blocked == [BlockedProperty("supplier_key", "引用类未配置主键列映射")]


def test_type_mismatch_severity_is_low():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="amount", source_column="AMOUNT", data_type="DECIMAL",
        is_primary_key=False)], [], SCHEMA)
    regex = [s for s in sugg if s.derivation_type == DerivationType.LLM_DERIVED]
    assert len(regex) == 1
    assert regex[0].severity == Severity.LOW
    assert regex[0].threshold == Decimal("95")


def test_all_generated_expressions_pass_validation():
    ref_master = ClassContext(class_id=2, class_name="Supplier", source_table="BPSUPPLIER",
                              object_type="Master")
    ref_dict = ClassContext(class_id=3, class_name="PoStatusDict", source_table="PO_STATUS",
                            object_type="Reference")
    edge = JoinEdgeMeta(target_table="PORDERQ", source_columns=["PO_KEY"],
                        target_columns=["PO_KEY"], target_date_columns=["RECEIPT_DATE"])
    props = [
        _prop(),
        _prop(property_name="status", source_column="STATUS", is_primary_key=False,
              allowed_values=["NEW", "CONFIRMED"]),
        _prop(property_name="supplier_key", source_column="SUPPLIER_KEY",
              is_primary_key=False, is_foreign_key=True, ref_class=ref_master,
              ref_key_column="SUPPLIER_KEY"),
        _prop(property_name="status", source_column="STATUS",
              is_primary_key=False, is_foreign_key=True, ref_class=ref_dict,
              ref_key_column="STATUS"),
        _prop(property_name="amount", source_column="AMOUNT", data_type="DECIMAL",
              is_primary_key=False),
        _prop(property_name="po_date", source_column="PO_DATE", data_type="DATETIME",
              is_primary_key=False),
    ]
    sugg, blocked = deriveSuggestions(CTX, props, [edge], SCHEMA)
    assert not blocked
    for s in sugg:
        assert s.rule_expression is not None
        validate_expression(s.rule_expression)
