"""P4a 时间参数化渲染 —— Oracle DATE 字面量 + 非 Oracle fail-closed（D4 裁决）。"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.time_scope_resolver import parseTimeScope
from app.services.time_scope_renderer import (
    REASON_TIME_SCOPE_PARSE_FAILED,
    REASON_TIME_SCOPE_RENDER_UNSUPPORTED,
    TIME_SCOPE_PLACEHOLDER,
    TimeScopeRenderUnsupportedError,
    prepareTimeParameterizedFormula,
    renderTimeScopePredicate,
)


class TestRenderTimeScopePredicate:
    def test_oracle_half_open_literal_window(self) -> None:
        """整段子句断言（防漂移假绿）：近6个月 ⇒ [2026-06-01, 2026-12-01)。"""
        scope = parseTimeScope("近6个月", today=date(2026, 11, 13))
        predicate = renderTimeScopePredicate(scope, "INVOICE_DATE", "oracle")
        assert predicate == (
            "INVOICE_DATE >= DATE '2026-06-01' AND INVOICE_DATE < DATE '2026-12-01'"
        )

    def test_dialect_is_normalized_case_insensitive(self) -> None:
        scope = parseTimeScope("本月", today=date(2026, 11, 13))
        predicate = renderTimeScopePredicate(scope, "CREATE_TIME", "ORACLE")
        assert predicate == (
            "CREATE_TIME >= DATE '2026-11-01' AND CREATE_TIME < DATE '2026-12-01'"
        )

    @pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
    def test_unsupported_dialect_raises(self, dialect: str) -> None:
        scope = parseTimeScope("近6个月", today=date(2026, 11, 13))
        with pytest.raises(TimeScopeRenderUnsupportedError):
            renderTimeScopePredicate(scope, "INVOICE_DATE", dialect)


class TestPrepareTimeParameterizedFormula:
    def test_placeholder_replaced_with_predicate(self) -> None:
        """ratio 型公式：谓词落 WHERE（聚合前行级过滤，D2 裁决的语义基础）。"""
        formula = (
            "SELECT COUNT(CASE WHEN STATUS = 'OK' THEN 1 END) / NULLIF(COUNT(*), 0) "
            "FROM ORDERS WHERE {time_scope}"
        )
        result = prepareTimeParameterizedFormula(
            formula=formula, question="近6个月准时交付率",
            timeColumn="INVOICE_DATE", dialect="oracle", today=date(2026, 11, 13),
        )
        assert result.ok is True
        assert result.scopeLabel == "近6个月"
        assert TIME_SCOPE_PLACEHOLDER not in (result.formula or "")
        assert (
            "ORDERS WHERE (INVOICE_DATE >= DATE '2026-06-01' "
            "AND INVOICE_DATE < DATE '2026-12-01')" in (result.formula or "")
        )

    def test_parse_failure_reason(self) -> None:
        result = prepareTimeParameterizedFormula(
            formula="SELECT 1 FROM T WHERE {time_scope}",
            question="2026-01-01 到 2026-03-31 的交付率",  # 具体区间不解析（D3）
            timeColumn="INVOICE_DATE", dialect="oracle", today=date(2026, 11, 13),
        )
        assert result.ok is False
        assert result.formula is None
        assert result.reason == REASON_TIME_SCOPE_PARSE_FAILED

    def test_unsupported_dialect_reason(self) -> None:
        result = prepareTimeParameterizedFormula(
            formula="SELECT 1 FROM T WHERE {time_scope}",
            question="近6个月交付率",
            timeColumn="INVOICE_DATE", dialect="postgresql", today=date(2026, 11, 13),
        )
        assert result.ok is False
        assert result.formula is None
        assert result.reason == REASON_TIME_SCOPE_RENDER_UNSUPPORTED

    def test_placeholder_absent_is_idempotent_pass_through(self) -> None:
        """防御：占位符不在 ⇒ 原样放行（路由层本不该调到这里）。"""
        formula = "SELECT COUNT(*) FROM T"
        result = prepareTimeParameterizedFormula(
            formula=formula, question="近6个月交付率",
            timeColumn="INVOICE_DATE", dialect="oracle", today=date(2026, 11, 13),
        )
        assert result.ok is True
        assert result.formula == formula
