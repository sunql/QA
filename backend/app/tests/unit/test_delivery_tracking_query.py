"""到货跟踪工具单测：窗口解析 + SQL 模板（本体属性名 → 物理列）。

窗口口径（spec 4.3，左闭右开）：
- 显式区间 > 24 日锚点 > monthsBack；
- 本期 = **已结束的最近一个完整锚点周期**（今天 2026-11-25 ⇒ 2026-10-24~2026-11-24）。

SQL 口径（spec 4.1/4.2）：
- 已收量 = 收货类按 PO_NO+PO_LINE_NO 聚合；
- 行级欠交量 = 订购量 - 已收量；订单三态按订单聚合判定；
- 表名/列名一律来自 `ResolvedBinding`，不硬编码。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from app.domain.exceptions import ValidationError
from app.services.delivery_tracking_query import resolveWindow
from app.services.ontology_binding_resolver import ResolvedBinding


def _orderBinding() -> ResolvedBinding:
    """订单行类绑定（属性名 → 物理列；本任务所需属性全给）。"""
    return ResolvedBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_PURCHASE_ORDER_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "PO_QTY": "PO_QTY",
            "ORDER_DATE": "ORDER_DATE",
            "SUPPLIER_CODE": "SUPPLIER_CODE",
            "ITEM_CODE": "ITEM_CODE",
            "EXP_RCV_DATE": "EXP_RCV_DATE",
        },
    )


def _receiptBinding() -> ResolvedBinding:
    """收货行类绑定（含物料描述与收货日期：byOrder 要展示）。"""
    return ResolvedBinding(
        className="DWD_GOODS_RECEIPT_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_GOODS_RECEIPT_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
            "RCV_DATE": "RCV_DATE",
            "ITEM_DESC": "ITEM_DESC",
        },
    )


# ---------------------------------------------------------------- 窗口解析


def test_months_back_window_is_today_minus_n_months() -> None:
    window = resolveWindow(today=date(2026, 11, 25), monthsBack=12)

    assert window.dateFrom == date(2025, 11, 25)
    assert window.dateTo == date(2026, 11, 25)
    assert window.mode == "monthsBack"
    assert window.anchorDay is None and window.periodIndex is None


def test_months_back_clamps_to_month_end() -> None:
    """3-31 往前 1 月 ⇒ 2-28（月末夹齐，不让 2 月缺 31 日炸掉）。"""
    window = resolveWindow(today=date(2026, 3, 31), monthsBack=1)

    assert window.dateFrom == date(2026, 2, 28)
    assert window.dateTo == date(2026, 3, 31)


def test_months_back_defaults_to_twelve() -> None:
    window = resolveWindow(today=date(2026, 11, 25))

    assert window.dateFrom == date(2025, 11, 25)
    assert window.dateTo == date(2026, 11, 25)
    assert window.mode == "monthsBack"


@pytest.mark.parametrize("badMonths", [0, 37, -1])
def test_months_back_rejects_out_of_range(badMonths: int) -> None:
    """超出 1..36 ⇒ 显式失败，不静默裁剪。"""
    with pytest.raises(ValidationError, match="monthsBack 必须是 1..36"):
        resolveWindow(today=date(2026, 11, 25), monthsBack=badMonths)


def test_months_back_rejects_non_integer() -> None:
    with pytest.raises(ValidationError, match="monthsBack 必须是 1..36"):
        resolveWindow(today=date(2026, 11, 25), monthsBack="12")  # type: ignore[arg-type]


def test_explicit_range_wins_over_months_back() -> None:
    window = resolveWindow(
        today=date(2026, 11, 25),
        monthsBack=12,
        dateFrom="2026-10-24",
        dateTo="2026-11-24",
    )

    assert window.dateFrom == date(2026, 10, 24)
    assert window.dateTo == date(2026, 11, 24)
    assert window.mode == "explicit"
    assert window.anchorDay is None and window.periodIndex is None


def test_explicit_range_wins_over_anchor_day() -> None:
    window = resolveWindow(
        today=date(2026, 11, 25),
        anchorDay=24,
        periodIndex=0,
        dateFrom="2026-01-01",
        dateTo="2026-02-01",
    )

    assert window.dateFrom == date(2026, 1, 1)
    assert window.dateTo == date(2026, 2, 1)
    assert window.mode == "explicit"
    assert window.anchorDay is None and window.periodIndex is None


@pytest.mark.parametrize("side", ["dateFrom", "dateTo"])
def test_explicit_range_requires_both_sides(side: str) -> None:
    """单边区间两条路径都 fail-closed（不静默忽略）。"""
    kwargs = {"today": date(2026, 11, 25), side: "2026-10-24"}
    with pytest.raises(ValidationError, match="dateFrom 与 dateTo 必须成对给出"):
        resolveWindow(**kwargs)  # type: ignore[arg-type]


def test_explicit_range_rejects_empty_string() -> None:
    """空串也是"给了"，不能当没给而回落 monthsBack。"""
    with pytest.raises(ValidationError, match="dateFrom 日期格式应为 YYYY-MM-DD"):
        resolveWindow(today=date(2026, 11, 25), dateFrom="", dateTo="2026-11-24")


def test_explicit_range_rejects_bad_format() -> None:
    with pytest.raises(ValidationError, match="dateFrom 日期格式应为 YYYY-MM-DD"):
        resolveWindow(today=date(2026, 11, 25), dateFrom="2026/10/24", dateTo="2026-11-24")


def test_explicit_range_rejects_impossible_calendar_date() -> None:
    """格式对但日历上不存在 ⇒ 同样 fail-closed（不许静默回退）。"""
    with pytest.raises(ValidationError, match="dateTo 不是有效日期"):
        resolveWindow(today=date(2026, 11, 25), dateFrom="2026-02-01", dateTo="2026-02-30")


def test_explicit_range_rejects_reversed_bounds() -> None:
    with pytest.raises(ValidationError, match="不能晚于"):
        resolveWindow(today=date(2026, 11, 25), dateFrom="2026-11-24", dateTo="2026-10-24")


def test_anchor_day_current_period_is_last_closed_cycle() -> None:
    """本期 = 已结束的最近一个完整 24 日周期（今天 11-25 ⇒ 10-24~11-24）。"""
    window = resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=0)

    assert window.dateFrom == date(2026, 10, 24)
    assert window.dateTo == date(2026, 11, 24)
    assert window.mode == "anchorDay"
    assert window.anchorDay == 24
    assert window.periodIndex == 0


def test_anchor_day_period_index_defaults_to_current() -> None:
    window = resolveWindow(today=date(2026, 11, 25), anchorDay=24)

    assert window.dateFrom == date(2026, 10, 24)
    assert window.dateTo == date(2026, 11, 24)
    assert window.periodIndex == 0


def test_anchor_day_previous_period() -> None:
    window = resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=-1)

    assert window.dateFrom == date(2026, 9, 24)
    assert window.dateTo == date(2026, 10, 24)
    assert window.periodIndex == -1


def test_anchor_day_on_exact_anchor_is_closed() -> None:
    """今天正好是锚点日 ⇒ 上一周期刚结束，本期 = 上一周期。"""
    window = resolveWindow(today=date(2026, 11, 24), anchorDay=24, periodIndex=0)

    assert window.dateFrom == date(2026, 10, 24)
    assert window.dateTo == date(2026, 11, 24)


def test_anchor_day_before_anchor_uses_last_month_anchor() -> None:
    """今天 1-05（锚点 24 尚未到）⇒ 本期 = 12-24 结束的那一期（未结束的周期不算）。"""
    window = resolveWindow(today=date(2027, 1, 5), anchorDay=24, periodIndex=0)

    assert window.dateFrom == date(2026, 11, 24)
    assert window.dateTo == date(2026, 12, 24)


def test_anchor_day_crosses_year_boundary() -> None:
    """12-24 锚点跨年：今天 2027-01-25（已过 1-24）⇒ 本期 = 2026-12-24 ~ 2027-01-24。"""
    window = resolveWindow(today=date(2027, 1, 25), anchorDay=24, periodIndex=0)

    assert window.dateFrom == date(2026, 12, 24)
    assert window.dateTo == date(2027, 1, 24)


def test_anchor_day_crosses_year_boundary_previous_period() -> None:
    window = resolveWindow(today=date(2027, 1, 25), anchorDay=24, periodIndex=-1)

    assert window.dateFrom == date(2026, 11, 24)
    assert window.dateTo == date(2026, 12, 24)


def test_anchor_day_february_short_month() -> None:
    """锚点 28 在 2 月短月正常（spec 4.3 实测 2026-02-28~2026-03-28）。"""
    window = resolveWindow(today=date(2026, 3, 30), anchorDay=28, periodIndex=0)

    assert window.dateFrom == date(2026, 2, 28)
    assert window.dateTo == date(2026, 3, 28)


@pytest.mark.parametrize("badAnchor", [0, 29, 31, -1])
def test_anchor_day_rejects_out_of_range(badAnchor: int) -> None:
    with pytest.raises(ValidationError, match="anchorDay 必须是 1..28"):
        resolveWindow(today=date(2026, 11, 25), anchorDay=badAnchor, periodIndex=0)


def test_anchor_day_rejects_non_integer() -> None:
    with pytest.raises(ValidationError, match="anchorDay 必须是 1..28"):
        resolveWindow(today=date(2026, 11, 25), anchorDay="24", periodIndex=0)  # type: ignore[arg-type]


def test_period_index_requires_anchor_day() -> None:
    """periodIndex 单独给出会被静默忽略 ⇒ 改为 fail-closed。"""
    with pytest.raises(ValidationError, match="periodIndex 必须与 anchorDay 一起使用"):
        resolveWindow(today=date(2026, 11, 25), periodIndex=0)


@pytest.mark.parametrize("futureIndex", [1, 2])
def test_anchor_day_rejects_future_period_index(futureIndex: int) -> None:
    """正数期序是未结束的未来周期 ⇒ fail-closed（模块不变式：只取已结束周期）。"""
    with pytest.raises(ValidationError, match="periodIndex 不能为正数"):
        resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=futureIndex)


def test_anchor_day_rejects_non_integer_period_index() -> None:
    with pytest.raises(ValidationError, match="periodIndex 必须是整数"):
        resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex="0")  # type: ignore[arg-type]


def test_window_is_immutable() -> None:
    """全局不可变约束：窗口对象创建后不得原地改。"""
    window = resolveWindow(today=date(2026, 11, 25), monthsBack=12)

    with pytest.raises(FrozenInstanceError):
        window.mode = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------- SQL 模板


def _renamedBindings() -> tuple[ResolvedBinding, ResolvedBinding]:
    """表名/列名全部改名的绑定：用来证明 SQL 没有硬编码物理名。"""
    order = ResolvedBinding(
        className="ORDER_ALIAS",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="ORD_VIEW",
        columns={
            "PO_NO": "PONO_X",
            "PO_LINE_NO": "POLINE_X",
            "PO_QTY": "QTY_X",
            "ORDER_DATE": "ODT_X",
            "SUPPLIER_CODE": "SUP_X",
            "ITEM_CODE": "ITEM_X",
            "EXP_RCV_DATE": "EXP_X",
        },
    )
    receipt = ResolvedBinding(
        className="RECEIPT_ALIAS",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="RCV_VIEW",
        columns={
            "PO_NO": "PONO_X",
            "PO_LINE_NO": "POLINE_X",
            "RCV_QTY_STU": "RQ_X",
            "RCV_DATE": "RD_X",
            "ITEM_DESC": "DESC_X",
        },
    )
    return order, receipt


def _allSqls(order: ResolvedBinding, receipt: ResolvedBinding, **kwargs: object) -> dict[str, str]:
    """三段 SQL（可带过滤参数），供多处断言共用。"""
    from app.services.delivery_tracking_query import (
        buildDeliveryTrackingByOrderSql,
        buildDeliveryTrackingBySupplierSql,
        buildDeliveryTrackingSummarySql,
    )

    window = resolveWindow(today=date(2026, 11, 25), monthsBack=12)
    return {
        "summary": buildDeliveryTrackingSummarySql(order=order, receipt=receipt, window=window),
        "bySupplier": buildDeliveryTrackingBySupplierSql(
            order=order,
            receipt=receipt,
            window=window,
            **kwargs,  # type: ignore[arg-type]
        ),
        "byOrder": buildDeliveryTrackingByOrderSql(
            order=order,
            receipt=receipt,
            window=window,
            **kwargs,  # type: ignore[arg-type]
        ),
    }


#: 脏行判据（欠交量 ≥ 1e6，**不是**订购量 —— spec 4.2/4.4 的关键区别）。
_DIRTY_OPEN = "O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0) >= 1000000"


def _normSql(sql: str) -> str:
    """空白归一化：SQL 片段断言不依赖换行/缩进。"""
    return " ".join(sql.split())


def _assertDirtyExclusion(sql: str, alias: str) -> None:
    """钉住 spec 4.4 的完整保证：脏行计数为 1，且不混入已收量/欠交行/已收行与订单分母。"""
    norm = _normSql(sql)
    # 脏行确实被**计数**（否则 EXCLUDED_ROWS 恒 0 —— 死字段）
    assert f"SUM(CASE WHEN {_DIRTY_OPEN} THEN 1 ELSE 0 END) AS DIRTY_LINES" in norm
    # 脏行不计入已收量（否则量级率被污染）
    assert f"SUM(CASE WHEN {_DIRTY_OPEN} THEN 0 ELSE COALESCE(R.RECEIVED_QTY, 0) END) AS RECEIVED_QTY" in norm
    # 脏行不计入欠交行（否则三态被污染）
    assert (
        f"SUM(CASE WHEN {_DIRTY_OPEN} THEN 0"
        " WHEN O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0) > 0"
        " THEN 1 ELSE 0 END) AS OPEN_LINES" in norm
    )
    # 脏行不计入已收行（否则三态被污染）
    assert (
        f"SUM(CASE WHEN {_DIRTY_OPEN} THEN 0"
        " WHEN COALESCE(R.RECEIVED_QTY, 0) > 0"
        " THEN 1 ELSE 0 END) AS RECEIVED_LINES" in norm
    )
    # 全脏行订单不进订单分母
    assert f"SUM(CASE WHEN {alias}.LINE_COUNT > 0 THEN 1 ELSE 0 END) AS TOTAL_ORDERS" in norm


def test_summary_sql_uses_bound_tables_and_columns() -> None:
    """SQL 里的表名列名全部来自绑定解析结果，不是硬编码。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingSummarySql

    window = resolveWindow(today=date(2026, 11, 25), anchorDay=24, periodIndex=0)
    sql = buildDeliveryTrackingSummarySql(order=_orderBinding(), receipt=_receiptBinding(), window=window)

    assert "FROM THBI.DWD_PURCHASE_ORDER_DTL O" in sql
    assert "FROM THBI.DWD_GOODS_RECEIPT_DTL GROUP BY" in sql
    assert "O.PO_QTY" in sql
    assert "SUM(RCV_QTY_STU) AS RECEIVED_QTY" in sql
    # 左闭右开
    assert "ORDER_DATE >= DATE '2026-10-24'" in sql
    assert "ORDER_DATE < DATE '2026-11-24'" in sql
    assert "ORDER_DATE <= DATE '2026-11-24'" not in sql


def test_summary_sql_exposes_handler_contract_and_three_states() -> None:
    """别名是 Task 4 `_assembleSummary` 的契约；三态在 SQL 里有明确分支。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingSummarySql

    sql = buildDeliveryTrackingSummarySql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    for alias in (
        "TOTAL_LINES",
        "TOTAL_ORDERS",
        "ORDERED_QTY",
        "RECEIVED_QTY",
        "OPEN_LINES",
        "RECEIVED_LINES",
        "ALL_ARRIVED",
        "PARTIAL",
        "NOT_ARRIVED",
    ):
        assert f"AS {alias}" in sql, f"summary SQL 缺别名 {alias}"
    # 订单级聚合：三态判据必须按 PO_NO 聚合后的行级计数
    assert "GROUP BY O.PO_NO" in sql
    assert "WHEN COALESCE(R.RECEIVED_QTY, 0) > 0" in sql


def test_three_states_are_mutually_exclusive_and_exhaustive() -> None:
    """三态互斥：未到货必须**同时**有欠交行，否则与全到货重叠 ⇒ 到货率 > 100%。

    订购量 ≤ 0 且未收货的订单 `OPEN_LINES = 0` 且 `RECEIVED_LINES = 0`：
    旧口径（只看 `RECEIVED_LINES = 0`）会把它同时算进「全到货」与「未到货」。
    """
    from app.services.delivery_tracking_query import (
        buildDeliveryTrackingBySupplierSql,
        buildDeliveryTrackingSummarySql,
    )

    window = resolveWindow(today=date(2026, 11, 25), monthsBack=12)
    summarySql = buildDeliveryTrackingSummarySql(
        order=_orderBinding(), receipt=_receiptBinding(), window=window
    )
    supplierSql = buildDeliveryTrackingBySupplierSql(
        order=_orderBinding(), receipt=_receiptBinding(), window=window
    )

    # 全到货 = 无欠交（覆盖「订购量 ≤ 0 且未收货」）；未到货 = 有欠交且零收货。
    assert "CASE WHEN T.LINE_COUNT > 0 AND T.OPEN_LINES = 0" in summarySql
    assert "CASE WHEN T.LINE_COUNT > 0 AND T.RECEIVED_LINES = 0 AND T.OPEN_LINES > 0" in summarySql
    assert "CASE WHEN S.LINE_COUNT > 0 AND S.RECEIVED_LINES = 0 AND S.OPEN_LINES > 0" in supplierSql
    # 反证：不允许「未到货」缺 OPEN_LINES 谓词（唯一会导致重复计数的形态）
    assert "CASE WHEN T.LINE_COUNT > 0 AND T.RECEIVED_LINES = 0 THEN" not in summarySql


def test_by_order_sql_carries_order_date_for_assembly() -> None:
    """明细 SQL 必须带下单日期：结果结构 `orderDate` 的来源（否则字段恒 null）。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "O.ORDER_DATE AS ORDER_DATE" in sql


def test_sql_renders_renamed_binding_and_never_erp_raw_names() -> None:
    """表名/列名只来自绑定；ERP 原始列名一个都不许出现。"""
    order, receipt = _renamedBindings()
    sqls = _allSqls(order, receipt)

    for name, sql in sqls.items():
        assert "THBI.ORD_VIEW" in sql, name
        assert "THBI.RCV_VIEW" in sql, name
        assert "DWD_PURCHASE_ORDER_DTL" not in sql, name
        assert "DWD_GOODS_RECEIPT_DTL" not in sql, name
        # 本体属性名也不该直接当物理列用（只允许作为结果列别名出现）
        assert "O.PO_QTY" not in sql, name
        assert "ORDER_DATE >=" not in sql, name
        assert "ODT_X >= DATE '2025-11-25'" in sql, name
        assert "ODT_X < DATE '2026-11-25'" in sql, name
        assert "SUM(RQ_X) AS RECEIVED_QTY" in sql, name
        for raw in ("QTYSTU_0", "RCPQTYSTU_0", "POHNUM_0", "POPLIN_0", "BPSNUM_0"):
            assert raw not in sql, f"{name} 出现了 ERP 原始列名 {raw}"


def test_receipt_aggregate_carries_material_desc_and_last_receipt_date() -> None:
    """收货聚合必须带物料描述与最后收货日（byOrder 展示用）。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "MAX(ITEM_DESC) AS ITEM_DESC" in sql
    assert "MAX(RCV_DATE) AS RECEIPT_DATE" in sql
    assert "R.ITEM_DESC AS MATERIAL_DESC" in sql
    assert "R.RECEIPT_DATE AS RECEIPT_DATE" in sql


def test_by_supplier_sql_filters_supplier_codes() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingBySupplierSql

    sql = buildDeliveryTrackingBySupplierSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        supplierKeys=("B125", "C1"),
        limit=50,
    )

    assert "O.SUPPLIER_CODE IN ('B125','C1')" in sql
    assert "FETCH FIRST 50 ROWS ONLY" in sql


def test_by_supplier_sql_escapes_quotes_in_supplier_codes() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingBySupplierSql

    sql = buildDeliveryTrackingBySupplierSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        supplierKeys=("O'Hara",),
    )

    assert "O.SUPPLIER_CODE IN ('O''Hara')" in sql


def test_by_supplier_sql_omits_filter_when_no_codes() -> None:
    """空 supplierKeys ⇒ 不带 IN 谓词（不是 IN ()）。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingBySupplierSql

    sql = buildDeliveryTrackingBySupplierSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "SUPPLIER_CODE IN (" not in sql
    assert "ORDER_DATE >= DATE '2025-11-25'" in sql


def test_by_order_sql_carries_line_detail_columns() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        limit=50,
    )

    for token in (
        "PO_NO",
        "PO_LINE_NO",
        "SUPPLIER_CODE",
        "MATERIAL_CODE",
        "MATERIAL_DESC",
        "ORDER_QTY",
        "RECEIVED_QTY",
        "OPEN_QTY",
        "EXPECTED_DATE",
        "RECEIPT_DATE",
    ):
        assert token in sql, f"byOrder SQL 缺列 {token}"
    assert "ORDER BY OPEN_QTY DESC" in sql
    assert "FETCH FIRST 50 ROWS ONLY" in sql


def test_by_order_sql_combines_supplier_and_order_filters() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        supplierKeys=("B125",),
        orderKeys=("T1", "T2"),
    )

    assert "O.SUPPLIER_CODE IN ('B125')" in sql
    assert "O.PO_NO IN ('T1','T2')" in sql


def test_by_order_sql_escapes_quotes_in_order_keys() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        orderKeys=("T1' OR '1'='1",),
    )

    assert "O.PO_NO IN ('T1'' OR ''1''=''1')" in sql


@pytest.mark.parametrize("badKey", [None, 123, "", "   "])
def test_filter_keys_reject_non_string_or_blank(badKey: object) -> None:
    """不可信过滤键不得被静默转成 `'None'`/空串（会假装“查无数据”）。"""
    from app.services.delivery_tracking_query import (
        buildDeliveryTrackingByOrderSql,
        buildDeliveryTrackingBySupplierSql,
    )

    window = resolveWindow(today=date(2026, 11, 25), monthsBack=12)
    with pytest.raises(ValidationError, match="键值必须是非空字符串"):
        buildDeliveryTrackingBySupplierSql(
            order=_orderBinding(),
            receipt=_receiptBinding(),
            window=window,
            supplierKeys=(badKey,),  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="键值必须是非空字符串"):
        buildDeliveryTrackingByOrderSql(
            order=_orderBinding(),
            receipt=_receiptBinding(),
            window=window,
            orderKeys=(badKey,),  # type: ignore[arg-type]
        )


def test_mysql_dialect_uses_limit_tail() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        limit=7,
        dialect="mysql",
    )

    assert sql.endswith("LIMIT 7")
    assert "FETCH FIRST" not in sql


def test_unknown_dialect_fails_closed() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingSummarySql

    with pytest.raises(ValidationError, match="不支持的数据源方言"):
        buildDeliveryTrackingSummarySql(
            order=_orderBinding(),
            receipt=_receiptBinding(),
            window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
            dialect="postgresql",
        )


def test_limit_is_bounded_to_maximum() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        limit=999,
    )

    assert "FETCH FIRST 200 ROWS ONLY" in sql


def test_limit_rejects_non_integer() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    with pytest.raises(ValidationError, match="limit 必须是整数"):
        buildDeliveryTrackingByOrderSql(
            order=_orderBinding(),
            receipt=_receiptBinding(),
            window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
            limit="50",  # type: ignore[arg-type]
        )


def test_summary_sql_excludes_dirty_lines_and_counts_them() -> None:
    """spec 4.4：行级欠交量 ≥ 1e6 的录入错误行剔除 + `EXCLUDED_ROWS` 显式计数。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingSummarySql

    sql = buildDeliveryTrackingSummarySql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "AS EXCLUDED_ROWS" in sql
    assert "O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0) >= 1000000" in sql
    # 4.2 vs 4.4 的关键区别：判据是**欠交量**，不是**订购量**（大额订购是真实业务）
    assert "O.PO_QTY >= 1000000" not in sql
    _assertDirtyExclusion(sql, "T")
    # 脏行确实被排除在统计之外（不只是"计数"）
    assert (
        "SUM(CASE WHEN O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0) >= 1000000"
        " THEN 0 ELSE 1 END) AS LINE_COUNT" in sql
    )
    assert "THEN 0 ELSE O.PO_QTY END) AS ORDER_QTY" in sql
    # 剔除口径一致：TOTAL_LINES 只数有效行
    assert "SUM(CASE WHEN T.LINE_COUNT > 0 THEN T.LINE_COUNT ELSE 0 END) AS TOTAL_LINES" in sql
    # 全是脏行的订单不落入三态（也不进 TOTAL_ORDERS）
    assert "CASE WHEN T.LINE_COUNT > 0 AND T.OPEN_LINES = 0" in sql
    assert "CASE WHEN T.LINE_COUNT > 0 AND T.RECEIVED_LINES = 0" in sql


def test_by_supplier_sql_excludes_dirty_lines_and_counts_them() -> None:
    from app.services.delivery_tracking_query import buildDeliveryTrackingBySupplierSql

    sql = buildDeliveryTrackingBySupplierSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "AS EXCLUDED_ROWS" in sql
    assert "O.PO_QTY - COALESCE(R.RECEIVED_QTY, 0) >= 1000000" in sql
    assert "O.PO_QTY >= 1000000" not in sql
    assert "THEN 0 ELSE O.PO_QTY END) AS ORDERED_QTY" in sql
    _assertDirtyExclusion(sql, "S")
    assert "CASE WHEN S.LINE_COUNT > 0 AND S.OPEN_LINES = 0" in sql
    assert "CASE WHEN S.LINE_COUNT > 0 AND S.RECEIVED_LINES = 0" in sql


def test_by_order_sql_keeps_dirty_lines() -> None:
    """明细是给人看的：byOrder **不剔除**脏行（不做统计剔除，也不计 EXCLUDED_ROWS）。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql

    sql = buildDeliveryTrackingByOrderSql(
        order=_orderBinding(),
        receipt=_receiptBinding(),
        window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert ">= 1000000" not in sql
    assert "EXCLUDED_ROWS" not in sql
    assert "AS OPEN_QTY" in sql


def test_by_order_requires_material_desc_binding() -> None:
    """收货绑定缺 ITEM_DESC ⇒ fail-closed，且病因精确到类与属性。"""
    from app.services.delivery_tracking_query import buildDeliveryTrackingByOrderSql
    from app.services.ontology_binding_resolver import BindingResolutionError

    receipt = ResolvedBinding(
        className="DWD_GOODS_RECEIPT_DTL",
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_GOODS_RECEIPT_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
            "RCV_DATE": "RCV_DATE",
        },
    )

    with pytest.raises(
        BindingResolutionError,
        match="DWD_GOODS_RECEIPT_DTL 的绑定里没有属性 ITEM_DESC",
    ):
        buildDeliveryTrackingByOrderSql(
            order=_orderBinding(),
            receipt=receipt,
            window=resolveWindow(today=date(2026, 11, 25), monthsBack=12),
        )
