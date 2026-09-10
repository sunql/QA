"""Prompt template rule 4 CTE/ratio tests (Task 2.3 TDD).

Run: pytest backend/tests/unit/test_nl2sql_prompt_template.py -v
"""

import pytest

from app.services.nl2sql_service import Nl2SqlService, DataSourceType
from app.services.nl2sql_service import _SQL_DIALECTS


class TestPromptRule4CteRatio:
    """Rule 4 (ratio/percent derived metrics) should allow CTE formula syntax."""

    @pytest.fixture
    def nl2sql(self):
        return Nl2SqlService()

    @pytest.fixture
    def minimal_schema_text(self):
        return "PurchaseOrder(id, qty, received_qty)\n  - id: ID\n  - qty: 数量\n  - received_qty: 收货数量\n"

    def _system_prompt(self, nl2sql, schema_text):
        """Helper: call _buildPlanSystemPrompt with minimal required args."""
        dialect = _SQL_DIALECTS[DataSourceType.POSTGRESQL]
        return nl2sql._buildPlanSystemPrompt(
            schemaText=schema_text,
            dialect=dialect,
            schemaPrefix=None,
        )

    def _extract_rule4(self, prompt: str) -> str:
        """Extract rule 4 text from the plan system prompt."""
        lines = prompt.split("\n")
        buf = []
        in_rule4 = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("4. ") or (in_rule4 and stripped and stripped[0].isdigit() and stripped.split(".")[0].strip().isdigit()):
                if stripped.startswith("5. "):
                    break
                in_rule4 = True
            if in_rule4 and not stripped.startswith("5. "):
                buf.append(line)
        return "\n".join(buf)

    def test_prompt_rule_4_mentions_cte_form(self, nl2sql, minimal_schema_text):
        """Prompt 规则 4 应明确提及 CTE/WITH 形式用于派生指标。"""
        prompt = self._system_prompt(nl2sql, minimal_schema_text)
        rule4 = self._extract_rule4(prompt)
        combined = rule4.lower()
        # Rule 4 should mention CTE or WITH keyword
        assert "cte" in combined or "with " in combined, (
            f"Rule 4 should explicitly allow CTE/WITH syntax for ratio/percent metrics.\n"
            f"Rule 4 text:\n{rule4}"
        )

    def test_prompt_rule_4_mentions_ratio_or_percent(self, nl2sql, minimal_schema_text):
        """Prompt 规则 4 应在 ratio/percent 上下文中提及 CTE。"""
        prompt = self._system_prompt(nl2sql, minimal_schema_text)
        rule4 = self._extract_rule4(prompt)
        combined = rule4.lower()
        # Rule 4 should discuss ratio/percent
        has_ratio = any(kw in combined for kw in ["ratio", "percent", "占比", "比率"])
        assert has_ratio, (
            f"Rule 4 should discuss ratio/percent derived metrics.\nRule 4 text:\n{rule4}"
        )
        # Rule 4 should mention CTE alongside ratio/percent
        # (within the same rule 4 text block)
        has_cte = "cte" in combined or "with " in combined
        assert has_cte, (
            f"Rule 4 should mention CTE/WITH alongside ratio/percent discussion.\n"
            f"Rule 4 text:\n{rule4}"
        )

    def test_prompt_rule_4_lists_allowed_aggregates_in_cte(self, nl2sql, minimal_schema_text):
        """Rule 4 应列出 CTE final SELECT 允许的聚合函数。"""
        prompt = self._system_prompt(nl2sql, minimal_schema_text)
        rule4 = self._extract_rule4(prompt)
        combined = rule4.lower()
        # Rule 4 should mention aggregate functions allowed in CTE final SELECT
        has_aggregate_mention = any(
            fn in combined
            for fn in ["avg(", "sum(", "count(", "stddev", "variance", "median", "percentile"]
        )
        assert has_aggregate_mention, (
            f"Rule 4 should list aggregate functions (AVG/SUM/COUNT/STDDEV/VARIANCE/MEDIAN/PERCENTILE_CONT) "
            f"as allowed in CTE final SELECT.\nRule 4 text:\n{rule4}"
        )

    def test_prompt_rule_4_includes_po_completion_rate_example(self, nl2sql, minimal_schema_text):
        """Prompt 规则 4 末尾应包含 PO 完成率具体示例。"""
        prompt = self._system_prompt(nl2sql, minimal_schema_text)
        rule4 = self._extract_rule4(prompt)
        combined = rule4.lower()
        # Rule 4 should include the PO completion rate example
        has_example = any(
            kw in combined
            for kw in ["po_ratio", "completion_rate", "received_qualified_qty"]
        )
        assert has_example, (
            f"Rule 4 should include PO completion rate example with po_ratio/completion_rate/received_qualified_qty.\n"
            f"Rule 4 text:\n{rule4}"
        )

    def test_prompt_rule_4_allows_both_window_and_cte_forms(self, nl2sql, minimal_schema_text):
        """Prompt 规则 4 应同时允许窗口函数形式和 CTE 形式（不允许排他性限制）。"""
        prompt = self._system_prompt(nl2sql, minimal_schema_text)
        rule4 = self._extract_rule4(prompt)
        combined = rule4.lower()
        # Both "窗口函数结构" (window function) and "CTE" should be present
        has_window = "窗口函数结构" in combined
        has_cte = "cte" in combined or "with " in combined
        # And crucially, it should say CTE is an alternative, not prohibited
        # "不再受窗口函数结构限制" signals CTE is allowed
        assert has_window and has_cte, (
            f"Rule 4 should allow both window function form AND CTE form.\n"
            f"has_window={has_window}, has_cte={has_cte}\nRule 4 text:\n{rule4}"
        )
        # The phrase "不再受窗口函数结构限制" should appear (CTE is explicitly freed)
        assert "不再受窗口函数结构限制" in combined, (
            f"Rule 4 should explicitly state CTE is not restricted to window functions.\n"
            f"Rule 4 text:\n{rule4}"
        )
