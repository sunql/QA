"""formula_parser CTE + additional aggregate function tests (Phase 2.2 TDD).

Run: pytest backend/tests/unit/test_formula_parser_cte.py -v
"""

import pytest

from app.services.formula_parser import parseFormula, ParsedFormula


class TestSimpleAggregateBackwardCompatibility:
    """向后兼容：现有简单聚合解析不受影响。"""

    def test_parse_avg_ratio(self):
        result = parseFormula("AVG(received_qualified_qty / purchase_qty)")
        assert "AVG" in result.aggregate_functions
        col_names = [col for _, col in result.column_refs]
        assert "received_qualified_qty" in col_names
        assert "purchase_qty" in col_names

    def test_parse_sum_with_alias(self):
        result = parseFormula("SUM(t.ORDER_QTY)")
        assert "SUM" in result.aggregate_functions
        assert ("t", "ORDER_QTY") in result.column_refs

    def test_parse_count_star(self):
        result = parseFormula("COUNT(*)")
        assert "COUNT" in result.aggregate_functions
        # COUNT(*) has no column refs
        assert result.column_refs == ()

    def test_parse_null_none_returns_empty(self):
        assert parseFormula(None) == ParsedFormula(column_refs=(), aggregate_functions=())
        assert parseFormula("") == ParsedFormula(column_refs=(), aggregate_functions=())
        assert parseFormula("   ") == ParsedFormula(column_refs=(), aggregate_functions=())


class TestAggregateFunctionsComplete:
    """所有 _AGGREGATE_FUNCTIONS 都能被识别。"""

    @pytest.mark.parametrize("agg", ["SUM", "AVG", "COUNT", "MAX", "MIN"])
    def test_standard_aggregates(self, agg):
        result = parseFormula(f"{agg}(col)")
        assert agg in result.aggregate_functions

    @pytest.mark.parametrize("agg", ["STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP", "MEDIAN", "PERCENTILE_CONT"])
    def test_extended_aggregates(self, agg):
        result = parseFormula(f"{agg}(amount)")
        assert agg in result.aggregate_functions, f"{agg} not recognised"


class TestCteFormula:
    """CTE 公式解析：WITH ... SELECT AGG(col) FROM cte_name GROUP BY ...""" ""

    def test_cte_detected(self):
        formula = """
        WITH line_ratios AS (
          SELECT supplier_id,
                 received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio
          FROM po_lines
          WHERE received_qualified_qty > 0
        )
        SELECT supplier_id, AVG(ratio) AS completion_rate
        FROM line_ratios
        GROUP BY supplier_id
        """
        result = parseFormula(formula)
        assert result.is_cte is True

    def test_cte_extracts_aggregate(self):
        formula = """
        WITH line_ratios AS (
          SELECT supplier_id, received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio
          FROM po_lines
        )
        SELECT supplier_id, AVG(ratio) AS completion_rate
        FROM line_ratios
        GROUP BY supplier_id
        """
        result = parseFormula(formula)
        assert "AVG" in result.aggregate_functions

    def test_cte_extracts_columns_from_final_select(self):
        formula = """
        WITH line_ratios AS (
          SELECT supplier_id, received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio
          FROM po_lines
        )
        SELECT supplier_id, AVG(ratio) AS completion_rate
        FROM line_ratios
        GROUP BY supplier_id
        """
        result = parseFormula(formula)
        # supplier_id from final SELECT (no alias, so alias="")
        assert ("", "supplier_id") in result.column_refs or ("supplier_id", "supplier_id") in result.column_refs
        # ratio from AVG(ratio) — the alias in the CTE inner SELECT
        # The parser should pick up "ratio" from the formula text
        col_names = [col for _, col in result.column_refs]
        assert "ratio" in col_names or "completion_rate" in col_names

    def test_cte_with_stddev(self):
        formula = """
        WITH order_amounts AS (
          SELECT supplier_id, amount
          FROM po_headers
        )
        SELECT supplier_id, STDDEV(amount) AS amount_stddev
        FROM order_amounts
        GROUP BY supplier_id
        """
        result = parseFormula(formula)
        assert result.is_cte is True
        assert "STDDEV" in result.aggregate_functions

    def test_cte_with_multiple_aggregates(self):
        formula = """
        WITH order_amounts AS (
          SELECT supplier_id, amount
          FROM po_headers
        )
        SELECT supplier_id, AVG(amount) AS avg_amount, STDDEV(amount) AS stddev_amount
        FROM order_amounts
        GROUP BY supplier_id
        """
        result = parseFormula(formula)
        assert result.is_cte is True
        assert "AVG" in result.aggregate_functions
        assert "STDDEV" in result.aggregate_functions

    def test_cte_name_extracted(self):
        formula = """
        WITH my_cte AS (SELECT x FROM t)
        SELECT AVG(x) FROM my_cte
        """
        result = parseFormula(formula)
        assert result.is_cte is True
        assert result.cte_name == "my_cte"


class TestParsedFormulaDataclass:
    """ParsedFormula 是不可变的。"""

    def test_is_frozen(self):
        result = parseFormula("AVG(col)")
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            result.aggregate_functions = ()
