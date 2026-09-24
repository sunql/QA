"""validatePlan CTE formula acceptance tests (Task 2.2 TDD).

Run: pytest backend/tests/unit/test_nl2sql_validate_plan_cte.py -v
"""

import pytest
from unittest.mock import MagicMock

from app.domain.query_plan import Aggregation, QueryPlan
from app.services.nl2sql_service import Nl2SqlService


def _make_property(name: str) -> MagicMock:
    """Build a minimal OntologyProperty mock with only the fields validatePlan reads."""
    prop = MagicMock()
    prop.property_name = name
    prop.property_alias = None
    prop.source_column = None
    prop.business_aliases = None
    return prop


def _make_class(name: str, properties: list[str]) -> MagicMock:
    """Build a minimal OntologyClass mock with the fields validatePlan reads."""
    cls = MagicMock()
    cls.class_name = name
    cls.source_table = None
    cls.properties = [_make_property(p) for p in properties]
    return cls


class TestValidatePlanCteFormula:
    """CTE formula + ratio/completion_rate alias should be accepted."""

    @pytest.fixture
    def nl2sql(self):
        return Nl2SqlService()

    @pytest.fixture
    def simple_class(self):
        """Minimal OntologyClass for property validation."""
        return _make_class("PurchaseOrder", ["supplier_id", "purchase_qty", "received_qualified_qty"])

    def test_validate_plan_accepts_cte_formula_for_ratio_alias(self, nl2sql, simple_class):
        """CTE 公式 + ratio/completion_rate alias 应被接受。

        Bug: _extractFormulaProperties 从 CTE inner SELECT 提取属性名（如 ratio），
        这些属性不在 owned（类属性）中，导致误报「公式中的属性不属于选定的任何类」。
        修复后 parseFormula(agg.formula).is_cte 为 True 时跳过该校验。
        """
        plan = QueryPlan(
            target="test",
            selectedClasses=["PurchaseOrder"],
            selectedProperties=["supplier_id"],
            aggregations=[
                Aggregation(
                    function="AVG",
                    property="supplier_id",
                    alias="completion_rate",
                    formula=(
                        "WITH line_ratios AS ("
                        "  SELECT supplier_id, received_qualified_qty / NULLIF(purchase_qty, 0) AS ratio "
                        "  FROM po_lines WHERE received_qualified_qty > 0"
                        ") "
                        "SELECT supplier_id, AVG(ratio) FROM line_ratios GROUP BY supplier_id"
                    ),
                )
            ],
            groupBy=(),
            sortBy=(),
        )
        issues = nl2sql.validatePlan(plan, [simple_class])
        # Should NOT have "formula 中的属性不属于" error for ratio
        formula_errors = [i for i in issues if "公式中的属性" in i]
        assert formula_errors == [], f"Unexpected formula errors: {formula_errors}"
        assert issues == []

    def test_validate_plan_cte_bypasses_formula_property_check(self, nl2sql, simple_class):
        """CTE 公式完全跳过公式属性校验（SQL Guard 已校验 CTE 语法）。

        Per brief: CTE 公式 return 后不再校验属性。
        inner SELECT 内部的名字（nonexistent_prop、po_lines）是 CTE 局部定义，
        不属于本体类属性，validatePlan 不对 CTE 内部做属性存在性校验。
        """
        plan = QueryPlan(
            target="test",
            selectedClasses=["PurchaseOrder"],
            selectedProperties=["supplier_id"],
            aggregations=[
                Aggregation(
                    function="AVG",
                    property="supplier_id",
                    alias="completion_rate",
                    formula=(
                        "WITH line_ratios AS ("
                        "  SELECT supplier_id, nonexistent_prop / NULLIF(purchase_qty, 0) AS ratio "
                        "  FROM po_lines"
                        ") "
                        "SELECT supplier_id, AVG(ratio) FROM line_ratios GROUP BY supplier_id"
                    ),
                )
            ],
            groupBy=(),
            sortBy=(),
        )
        issues = nl2sql.validatePlan(plan, [simple_class])
        # CTE inner names are NOT validated by validatePlan (SQL Guard handles CTE syntax)
        formula_errors = [i for i in issues if "公式中的属性" in i]
        assert formula_errors == [], f"CTE formula should bypass property check: {formula_errors}"

    def test_non_cte_formula_still_validates_properties(self, nl2sql, simple_class):
        """非 CTE 公式的属性校验逻辑不变（回归测试）。"""
        plan = QueryPlan(
            target="test",
            selectedClasses=["PurchaseOrder"],
            selectedProperties=["supplier_id"],
            aggregations=[
                Aggregation(
                    function="AVG",
                    property="supplier_id",
                    alias="total_ratio",
                    formula="SUM(received_qualified_qty) / NULLIF(SUM(purchase_qty), 0)",
                )
            ],
            groupBy=(),
            sortBy=(),
        )
        # received_qualified_qty and purchase_qty ARE in simple_class → should pass
        issues = nl2sql.validatePlan(plan, [simple_class])
        formula_errors = [i for i in issues if "公式中的属性" in i]
        assert formula_errors == [], f"Unexpected formula errors: {formula_errors}"

    def test_non_cte_formula_unknown_property_still_rejected(self, nl2sql, simple_class):
        """非 CTE 公式引用未知属性仍应拒绝（回归测试）。"""
        plan = QueryPlan(
            target="test",
            selectedClasses=["PurchaseOrder"],
            selectedProperties=["supplier_id"],
            aggregations=[
                Aggregation(
                    function="AVG",
                    property="supplier_id",
                    alias="total_ratio",
                    formula="SUM(nonexistent_col) / NULLIF(SUM(another_missing), 0)",
                )
            ],
            groupBy=(),
            sortBy=(),
        )
        issues = nl2sql.validatePlan(plan, [simple_class])
        assert any("公式中的属性 nonexistent_col 不属于" in i for i in issues), (
            f"Expected formula property validation error, got: {issues}"
        )

    def test_cte_formula_ratio_alias_no_formula_flag_error(self, nl2sql, simple_class):
        """CTE 公式不应触发「别名暗示派生指标但无 formula」报错。

        _aliasRequiresFormula 检查的是 not agg.formula；CTE 形式的 formula
        不应被视为「无 formula」。
        """
        plan = QueryPlan(
            target="test",
            selectedClasses=["PurchaseOrder"],
            selectedProperties=["supplier_id"],
            aggregations=[
                Aggregation(
                    function="AVG",
                    property="supplier_id",
                    alias="completion_rate",
                    formula=(
                        "WITH x AS (SELECT supplier_id, 1.0 AS ratio FROM po_lines) "
                        "SELECT supplier_id, AVG(ratio) FROM x GROUP BY supplier_id"
                    ),
                )
            ],
            groupBy=(),
            sortBy=(),
        )
        issues = nl2sql.validatePlan(plan, [simple_class])
        alias_errors = [i for i in issues if "派生指标" in i or "completion_rate" in i]
        assert alias_errors == [], f"Unexpected alias/formula errors: {alias_errors}"
