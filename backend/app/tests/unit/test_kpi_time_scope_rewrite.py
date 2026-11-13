"""P4a 存量改写建议（纯函数 dry-run，零 IO 零写入）。"""

from __future__ import annotations

from app.services.kpi_time_scope_rewrite import (
    ACTION_BLOCKED,
    ACTION_NOOP,
    ACTION_SUGGEST,
    TIME_SCOPE_PLACEHOLDER,
    buildTimeScopeRewriteSuggestions,
)


def _metric(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 2,
        "metric_name": "准时交付率",
        "execution_source": "ESCAPE_HATCH",
        "formula": "SELECT COUNT(CASE WHEN STATUS='OK' THEN 1 END)/NULLIF(COUNT(*),0) FROM ORDERS",
        "time_column": None,
        "status": "PUBLISHED",
    }
    base.update(overrides)
    return base


def test_no_placeholder_and_no_column_is_blocked() -> None:
    """R3 不猜：列缺失不给建议文本，报告 blocked。"""
    (s,) = buildTimeScopeRewriteSuggestions([_metric()])
    assert s.action == ACTION_BLOCKED
    assert s.suggestedFormula is None
    assert s.reason is not None


def test_with_column_appends_where_clause() -> None:
    """无 WHERE 的聚合公式 ⇒ 追加 ` WHERE ({占位符})`（行级过滤在聚合前）。"""
    (s,) = buildTimeScopeRewriteSuggestions(
        [_metric(time_column="INVOICE_DATE")]
    )
    assert s.action == ACTION_SUGGEST
    assert s.suggestedFormula == (
        "SELECT COUNT(CASE WHEN STATUS='OK' THEN 1 END)/NULLIF(COUNT(*),0) FROM ORDERS"
        f" WHERE ({TIME_SCOPE_PLACEHOLDER})"
    )


def test_with_column_and_where_inserts_after_first_where() -> None:
    (s,) = buildTimeScopeRewriteSuggestions(
        [_metric(time_column="INVOICE_DATE",
                 formula="SELECT COUNT(*) FROM ORDERS WHERE STATUS='OK'")]
    )
    assert s.action == ACTION_SUGGEST
    assert s.suggestedFormula == (
        f"SELECT COUNT(*) FROM ORDERS WHERE ({TIME_SCOPE_PLACEHOLDER}) AND STATUS='OK'"
    )


def test_existing_placeholder_is_noop() -> None:
    (s,) = buildTimeScopeRewriteSuggestions(
        [_metric(time_column="INVOICE_DATE",
                 formula=f"SELECT COUNT(*) FROM T WHERE ({TIME_SCOPE_PLACEHOLDER})")]
    )
    assert s.action == ACTION_NOOP
    assert s.suggestedFormula is None


def test_non_escape_hatch_metrics_are_excluded() -> None:
    suggestions = buildTimeScopeRewriteSuggestions(
        [_metric(execution_source="COMPILED", time_column="INVOICE_DATE")]
    )
    assert suggestions == ()
