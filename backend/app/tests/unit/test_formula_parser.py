"""formula_parser 单元测试（Phase 2.2 RED）。

解析 SQL formula → 提取列引用 + 聚合函数列表，供 lineage_extractor
推断 Metric → Property 血缘边。

覆盖：
- 简单 SUM(col) / AVG(col) / COUNT(*)
- 表别名 t.col 与裸列名 col
- 算术表达式（+/-/*/// 与括号）
- CASE WHEN 内嵌列引用
- 嵌套子查询（best-effort：仅提取顶层列）
- 字符串/数字常量（应被忽略）
- 异常输入：空字符串、None、语法错误
"""

from __future__ import annotations

import pytest

from app.services.formula_parser import (
    ParsedFormula,
    parseFormula,
)


class TestParseSimpleAggregate:
    def test_sum_with_alias(self):
        result = parseFormula("SUM(t.ORDER_QTY)")
        assert result.aggregate_functions == ("SUM",)
        assert ("t", "ORDER_QTY") in result.column_refs

    def test_avg_with_alias(self):
        result = parseFormula("AVG(t.PRICE)")
        assert "AVG" in result.aggregate_functions
        assert ("t", "PRICE") in result.column_refs

    def test_count_star(self):
        result = parseFormula("COUNT(*)")
        assert "COUNT" in result.aggregate_functions
        # COUNT(*) 不暴露任何列引用
        assert result.column_refs == ()

    def test_count_column(self):
        result = parseFormula("COUNT(DISTINCT t.SUPPLIER_ID)")
        assert "COUNT" in result.aggregate_functions
        assert ("t", "SUPPLIER_ID") in result.column_refs

    def test_max_min(self):
        result = parseFormula("MAX(t.ORDER_DATE) - MIN(t.DELIVERY_DATE)")
        assert {"MAX", "MIN"}.issubset(set(result.aggregate_functions))
        assert ("t", "ORDER_DATE") in result.column_refs
        assert ("t", "DELIVERY_DATE") in result.column_refs


class TestParseArithmetic:
    def test_two_columns_multiplied(self):
        result = parseFormula("SUM(t.QTY) * AVG(t.PRICE)")
        assert ("t", "QTY") in result.column_refs
        assert ("t", "PRICE") in result.column_refs
        assert {"SUM", "AVG"}.issubset(set(result.aggregate_functions))

    def test_parenthesized_expression(self):
        result = parseFormula("SUM(t.RECEIVED_QTY) / NULLIF(COUNT(t.ID), 0)")
        assert ("t", "RECEIVED_QTY") in result.column_refs
        assert ("t", "ID") in result.column_refs
        assert {"SUM", "COUNT"}.issubset(set(result.aggregate_functions))

    def test_bare_column_no_alias(self):
        result = parseFormula("SUM(ORDER_QTY)")
        # 无表别名时：表别名为空字符串，列名为 ORDER_QTY
        assert ("", "ORDER_QTY") in result.column_refs


class TestParseCaseWhen:
    def test_case_when_extracts_columns(self):
        formula = "SUM(CASE WHEN t.STATUS = 'CLOSED' THEN t.QTY ELSE 0 END)"
        result = parseFormula(formula)
        assert "SUM" in result.aggregate_functions
        assert ("t", "STATUS") in result.column_refs
        assert ("t", "QTY") in result.column_refs

    def test_nested_case(self):
        formula = "AVG(CASE WHEN t.A > 0 THEN t.B / t.C ELSE t.D END)"
        result = parseFormula(formula)
        assert ("t", "A") in result.column_refs
        assert ("t", "B") in result.column_refs
        assert ("t", "C") in result.column_refs
        assert ("t", "D") in result.column_refs


class TestParseConstants:
    def test_numeric_literal_ignored(self):
        result = parseFormula("SUM(t.QTY) * 1.1")
        assert ("t", "QTY") in result.column_refs
        # 数字常量不计入 column_refs
        assert all(ref[1] not in ("1", "1.1") for ref in result.column_refs)

    def test_string_literal_ignored(self):
        result = parseFormula("SUM(CASE WHEN t.STATUS = 'OPEN' THEN t.QTY END)")
        assert ("t", "STATUS") in result.column_refs
        assert ("t", "QTY") in result.column_refs

    def test_pure_constant_returns_empty(self):
        result = parseFormula("1 + 2 * 3")
        assert result.column_refs == ()
        assert result.aggregate_functions == ()


class TestParseSubquery:
    def test_subquery_columns_extracted_best_effort(self):
        """嵌套子查询：best-effort 提取列引用；不保证嵌套深度。"""
        formula = "SUM((SELECT AVG(t2.SUB_QTY) FROM t2 WHERE t2.ID = t.ID))"
        result = parseFormula(formula)
        assert "SUM" in result.aggregate_functions
        # 至少提取外层引用
        assert ("t", "ID") in result.column_refs


class TestParseWindowFunction:
    def test_window_function_columns(self):
        formula = "SUM(t.QTY) OVER (PARTITION BY t.SUPPLIER_ID ORDER BY t.ORDER_DATE)"
        result = parseFormula(formula)
        assert ("t", "QTY") in result.column_refs
        assert ("t", "SUPPLIER_ID") in result.column_refs
        assert ("t", "ORDER_DATE") in result.column_refs


class TestParseInvalid:
    def test_empty_string_returns_empty(self):
        result = parseFormula("")
        assert result.column_refs == ()
        assert result.aggregate_functions == ()

    def test_whitespace_only_returns_empty(self):
        result = parseFormula("   \n\t  ")
        assert result.column_refs == ()

    def test_none_safe(self):
        """None 输入：返回空 ParsedFormula（不抛错，调用方应自行 None 守卫）。"""
        result = parseFormula(None)  # type: ignore[arg-type]
        assert result.column_refs == ()
        assert result.aggregate_functions == ()


class TestParsedFormulaImmutability:
    def test_returns_named_tuple_with_frozen_fields(self):
        """ParsedFormula 应是不可变 dataclass / NamedTuple。"""
        result = parseFormula("SUM(t.QTY)")
        # tuple 是不可变的；确认类型
        assert isinstance(result.column_refs, tuple)
        assert isinstance(result.aggregate_functions, tuple)
        # 不允许修改
        with pytest.raises(Exception):  # noqa: PT011
            result.column_refs = (("x", "y"),)  # type: ignore[misc]


class TestParseAliasStyle:
    def test_dotted_alias_and_column(self):
        """识别 alias.column 形式。"""
        result = parseFormula("SUM(po.ORDER_QTY)")
        assert ("po", "ORDER_QTY") in result.column_refs

    def test_backtick_quote_identifier(self):
        """支持反引号包裹的标识符（MySQL 方言）。"""
        result = parseFormula("SUM(`po`.`ORDER_QTY`)")
        assert ("po", "ORDER_QTY") in result.column_refs

    def test_double_quote_identifier(self):
        """支持双引号包裹的标识符（PG 大小写敏感场景）。"""
        result = parseFormula('SUM("po"."ORDER_QTY")')
        assert ("po", "ORDER_QTY") in result.column_refs