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


# ---------------------------------------------------------------------------
# 存量 8 条 ESCAPE_HATCH 快照（Spec §7 输出稳定性）
#
# 数据镜像 dev-5433 qa_metadata.ontology_metric 实测行（只读 SELECT 取证于
# 2026-11-13，与 Task 6 live dry-run 输出一致）；纯 fixture，测试零 DB 依赖。
# 本测试是现有正确行为的 characterization：若改动建议逻辑导致此测试失败，
# 视为行为漂移，须显式评审而非默默改测试。
# ---------------------------------------------------------------------------

_ESCAPE_HATCH = "ESCAPE_HATCH"

_METRIC_2_FORMULA = (
    "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END)"
    " / NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0)"
    " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
    " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
    " ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
    " WHERE O.PROMISED_RECEIPT_DATE IS NOT NULL"
)
_METRIC_3_FORMULA = (
    "SELECT COUNT(CASE WHEN R.RECEIPT_DATE > O.PROMISED_RECEIPT_DATE THEN 1 END)"
    " / NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0)"
    " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
    " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
    " ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
    " WHERE O.PROMISED_RECEIPT_DATE IS NOT NULL"
)
_METRIC_4_FORMULA = (
    "SELECT COUNT(CASE WHEN VALUE_DATE <= DUE_DATE THEN 1 END)"
    " / NULLIF(COUNT(DUE_DATE), 0) FROM THBI.DWD_SUPPLIER_PAYMENT"
)
_METRIC_5_FORMULA = (
    "SELECT SUM(CASE WHEN IL.PO_NO = O.PO_NO AND IL.PO_LINE_NO = O.PO_LINE_NO"
    " AND IL.QUANTITY = O.ORDER_QTY AND IL.UNIT_PRICE_NET = O.UNIT_PRICE"
    " THEN IL.LINE_AMOUNT_EXCL_TAX ELSE 0 END)"
    " / NULLIF(SUM(I.AMOUNT_EXCL_TAX), 0)"
    " FROM THBI.DWD_PURCHASE_INVOICE I"
    " JOIN THBI.DWD_PURCHASE_INVOICE_LINE IL"
    " ON IL.INVOICE_TYPE = I.INVOICE_TYPE AND IL.INVOICE_NO = I.INVOICE_NO"
    " JOIN THBI.DWD_PURCHASE_ORDER_LINE O"
    " ON O.PO_NO = IL.PO_NO AND O.PO_LINE_NO = IL.PO_LINE_NO"
)
_METRIC_6_FORMULA = (
    "SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE))"
    " FROM THBI.DWD_REQUISITION_ORDER_LINK K"
    " JOIN THBI.DWD_PURCHASE_REQUISITION_LINE L"
    " ON K.REQUISITION_NO = L.REQUISITION_NO"
    " AND K.REQUISITION_LINE_NO = L.REQUISITION_LINE_NO"
    " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
    " ON K.PO_NO = R.PO_NO AND K.PO_LINE_NO = R.PO_LINE_NO"
)
_METRIC_7_FORMULA = (
    "SELECT AVG(TRUNC(R.RECEIPT_DATE) - TRUNC(O.PROMISED_RECEIPT_DATE))"
    " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
    " JOIN THBI.DWD_GOODS_RECEIPT_LINE R"
    " ON O.PO_NO = R.PO_NO AND O.PO_LINE_NO = R.PO_LINE_NO"
    " WHERE R.RECEIPT_DATE > O.PROMISED_RECEIPT_DATE"
)
_METRIC_8_FORMULA = (
    "SELECT SUM((P.UNIT_PRICE - O.UNIT_PRICE) * O.ORDER_QTY)"
    " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
    " JOIN THBI.DWD_SUPPLIER_PRICE_LIST P"
    " ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE"
    " AND P.VALID_FROM <= O.ORDER_DATE"
    " AND (P.VALID_TO IS NULL OR P.VALID_TO >= O.ORDER_DATE)"
)
_METRIC_9_FORMULA = (
    "SELECT AVG((O.UNIT_PRICE - P.UNIT_PRICE) / NULLIF(P.UNIT_PRICE, 0))"
    " FROM THBI.DWD_PURCHASE_ORDER_LINE O"
    " JOIN THBI.DWD_SUPPLIER_PRICE_LIST P"
    " ON P.MATERIAL_CODE = O.SUPPLIER_MATERIAL_CODE"
    " AND P.VALID_FROM <= O.ORDER_DATE"
    " AND (P.VALID_TO IS NULL OR P.VALID_TO >= O.ORDER_DATE)"
)

# (id, metric_name, formula, time_column) —— 与 dev-5433 实测行一一对应。
_DEV_STOCK_ROWS: tuple[tuple[int, str, str, str | None], ...] = (
    (2, "KPI_SUPPLIER_OTD 供应商准时交付率", _METRIC_2_FORMULA, None),
    (3, "KPI_SUPPLIER_OVERDUE_RATIO 供应商逾期率", _METRIC_3_FORMULA, None),
    (4, "KPI_PAYMENT_ON_TIME 付款及时率", _METRIC_4_FORMULA, None),
    (5, "KPI_INVOICE_MATCH_RATE 发票三单匹配率", _METRIC_5_FORMULA, "INVOICE_DATE"),
    (6, "KPI_PURCHASE_CYCLE_TIME 采购周期", _METRIC_6_FORMULA, None),
    (7, "KPI_SUPPLIER_DELAY_DAYS 供应商平均延误天数", _METRIC_7_FORMULA, None),
    (8, "KPI_COST_SAVING 采购成本节约", _METRIC_8_FORMULA, None),
    (9, "KPI_PURCHASE_PRICE_VARIANCE 采购价格偏差率", _METRIC_9_FORMULA, None),
)


def _stockMetrics() -> list[dict[str, object]]:
    return [
        {
            "id": metricId,
            "metric_name": metricName,
            "execution_source": _ESCAPE_HATCH,
            "formula": formula,
            "time_column": timeColumn,
            "status": "PUBLISHED",
        }
        for metricId, metricName, formula, timeColumn in _DEV_STOCK_ROWS
    ]


def test_stock_eight_metrics_snapshot() -> None:
    """存量 8 条 ESCAPE_HATCH：7 blocked + metric 5 suggest（完整建议文本钉死）。"""
    suggestions = buildTimeScopeRewriteSuggestions(_stockMetrics())

    assert len(suggestions) == 8
    assert [s.metricId for s in suggestions] == [2, 3, 4, 5, 6, 7, 8, 9]

    blocked = [s for s in suggestions if s.action == ACTION_BLOCKED]
    assert len(blocked) == 7
    assert {s.metricId for s in blocked} == {2, 3, 4, 6, 7, 8, 9}
    # R3 不猜：blocked 一律无建议文本，且原因必填。
    assert all(s.suggestedFormula is None for s in blocked)
    assert all(s.reason is not None for s in blocked)
    # 含 WHERE 的 2/3/7 也不给建议（blocked 优先于插入启发式）。
    assert all(s.timeColumn is None for s in blocked)

    (metric5,) = [s for s in suggestions if s.metricId == 5]
    assert metric5.action == ACTION_SUGGEST
    assert metric5.timeColumn == "INVOICE_DATE"
    assert metric5.suggestedFormula == _METRIC_5_FORMULA + f" WHERE ({TIME_SCOPE_PLACEHOLDER})"


def test_stock_suggestions_are_idempotent() -> None:
    """同一输入重复调用结果相等（frozen dataclass 元组相等，输出确定性）。"""
    metrics = _stockMetrics()
    first = buildTimeScopeRewriteSuggestions(metrics)
    second = buildTimeScopeRewriteSuggestions(metrics)
    assert first == second
    assert first == buildTimeScopeRewriteSuggestions(_stockMetrics())
