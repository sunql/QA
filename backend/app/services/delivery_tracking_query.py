"""到货跟踪工具（2026-11-25，P5）：取数窗口 + 三段只读 SQL 模板。

口径（spec 4.1 / 4.2，THBI 实测基线）
------------------------------------
    已收量（行级）  RECEIVED = SUM(<RCV_QTY_STU>)   -- 收货类按 PO_NO+PO_LINE_NO 聚合
    行级欠交量      OPEN_QTY = <PO_QTY> - RECEIVED
    订单三态        全到货 / 部分到货 / 未到货
    订单到货率      全到货订单 / 总订单              （主指标）
    量级率          SUM(RECEIVED) / SUM(<PO_QTY>)    （参考，受超大额未到货行影响）

窗口（spec 4.3，左闭右开）
--------------------------
- 优先级：显式 dateFrom/dateTo > 24 日锚点 > monthsBack（默认 12）；
- 本期 = **已结束的最近一个完整锚点周期**（今天 2026-11-25 ⇒ 2026-10-24~2026-11-24）；
- 锚点日上限 28（29/30/31 在短月无对应日）。

硬规则
------
- 一切对象出自 THBI（`datasource_id=1` + `schema_name=THBI`），由绑定解析保证；
- 物理表名/列名只来自本体绑定解析，**不进代码常量**；
- 失败一律显式抛出（非法参数 `ValidationError`，属性缺失 `BindingResolutionError`），
  不静默纠正、不静默忽略。
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from app.domain.exceptions import ValidationError
from app.services.ontology_binding_resolver import ResolvedBinding

ORDER_CLASS_NAME = "DWD_PURCHASE_ORDER_DTL"
RECEIPT_CLASS_NAME = "DWD_GOODS_RECEIPT_DTL"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: 锚点日上限 28：避免 29/30/31 在短月无对应日（spec 4.3）。
_MAX_ANCHOR_DAY = 28
_MIN_MONTHS_BACK = 1
_MAX_MONTHS_BACK = 36
_MAX_LIMIT = 200
#: 支持的方言：默认现役 Oracle；场景槽位可切 MySQL 源（与待到货工具一致）。
_SUPPORTED_DIALECTS = frozenset({"oracle", "mysql"})


@dataclass(frozen=True, slots=True)
class DeliveryTrackingWindow:
    """取数窗口（已解析为具体起止日；左闭右开）。"""

    dateFrom: date
    dateTo: date
    mode: str
    anchorDay: int | None = None
    periodIndex: int | None = None


# ---------------------------------------------------------------- 窗口解析


def _parseDate(value: object, field: str) -> date:
    """YYYY-MM-DD → date；格式/日历非法都 fail-closed。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(f"{field} 日期格式应为 YYYY-MM-DD，收到 {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是有效日期：{value!r}") from exc


def _shiftMonths(base: date, months: int) -> date:
    """base 偏移 months 个月（月末夹齐：3-31 减 1 月 = 2-28）。"""
    total = base.year * 12 + (base.month - 1) + months
    year, monthIndex = divmod(total, 12)
    month = monthIndex + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _anchorDate(today: date, anchorDay: int) -> date:
    """锚点基准日：当月锚点日；若已过（或正值当天），取当月；否则取上月锚点日。

    取「不晚于今天」的最近锚点日 ⇒ 以它为右端点的周期一定已经结束。
    """
    current = date(today.year, today.month, anchorDay)
    return current if current <= today else _shiftMonths(current, -1)


def _requireInt(value: object, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(message)
    return value


def _resolveExplicit(dateFrom: str, dateTo: str) -> DeliveryTrackingWindow:
    start = _parseDate(dateFrom, "dateFrom")
    end = _parseDate(dateTo, "dateTo")
    if start > end:
        raise ValidationError(f"dateFrom({start}) 不能晚于 dateTo({end})")
    return DeliveryTrackingWindow(dateFrom=start, dateTo=end, mode="explicit")


def _resolveAnchor(today: date, anchorDay: object, periodIndex: object) -> DeliveryTrackingWindow:
    anchor = _requireInt(
        anchorDay,
        f"anchorDay 必须是 1..{_MAX_ANCHOR_DAY} 之间的整数，收到 {anchorDay!r}",
    )
    if not 1 <= anchor <= _MAX_ANCHOR_DAY:
        raise ValidationError(f"anchorDay 必须是 1..{_MAX_ANCHOR_DAY} 之间的整数，收到 {anchorDay!r}")
    index = (
        0
        if periodIndex is None
        else _requireInt(periodIndex, f"periodIndex 必须是整数，收到 {periodIndex!r}")
    )
    base = _anchorDate(today, anchor)
    # 本期 = [anchor 上月同日, anchor)；periodIndex=-1 再往前一期。
    return DeliveryTrackingWindow(
        dateFrom=_shiftMonths(base, index - 1),
        dateTo=_shiftMonths(base, index),
        mode="anchorDay",
        anchorDay=anchor,
        periodIndex=index,
    )


def _resolveMonthsBack(today: date, monthsBack: object) -> DeliveryTrackingWindow:
    if monthsBack is None:
        months = 12
    else:
        months = _requireInt(
            monthsBack,
            f"monthsBack 必须是 {_MIN_MONTHS_BACK}..{_MAX_MONTHS_BACK} 之间的整数，收到 {monthsBack!r}",
        )
        if not _MIN_MONTHS_BACK <= months <= _MAX_MONTHS_BACK:
            raise ValidationError(
                f"monthsBack 必须是 {_MIN_MONTHS_BACK}..{_MAX_MONTHS_BACK} 之间的整数，收到 {monthsBack!r}"
            )
    return DeliveryTrackingWindow(dateFrom=_shiftMonths(today, -months), dateTo=today, mode="monthsBack")


def resolveWindow(
    *,
    today: date,
    monthsBack: int | None = None,
    dateFrom: str | None = None,
    dateTo: str | None = None,
    anchorDay: int | None = None,
    periodIndex: int | None = None,
) -> DeliveryTrackingWindow:
    """解析取数窗口（左闭右开）。

    优先级：显式区间 > 24 日锚点 > 自然月窗口。

    :raises ValidationError: 单边区间、日期格式/日历非法、区间反向、
        anchorDay 越界、periodIndex 缺 anchorDay、monthsBack 越界。
    """
    if dateFrom is not None or dateTo is not None:
        if dateFrom is None or dateTo is None:
            raise ValidationError("dateFrom 与 dateTo 必须成对给出（不支持单边区间）")
        return _resolveExplicit(dateFrom, dateTo)

    if anchorDay is not None:
        return _resolveAnchor(today, anchorDay, periodIndex)

    if periodIndex is not None:
        raise ValidationError("periodIndex 必须与 anchorDay 一起使用")

    return _resolveMonthsBack(today, monthsBack)


# ---------------------------------------------------------------- SQL 片段


def _esc(value: str) -> str:
    """字面量转义（单引号翻倍）。"""
    return value.replace("'", "''")


def _dialectName(dialect: str) -> str:
    """校验并归一化方言（现役 Oracle / 场景槽位可切 MySQL）。"""
    name = str(dialect or "").strip().lower()
    if name not in _SUPPORTED_DIALECTS:
        raise ValidationError(
            f"不支持的数据源方言：{dialect!r}（仅支持 {' / '.join(sorted(_SUPPORTED_DIALECTS))}）"
        )
    return name


def _rowLimit(limit: object, dialect: str) -> str:
    """行数限制尾巴：Oracle `FETCH FIRST n ROWS ONLY` / MySQL `LIMIT n`。

    `limit` 上界 200（防大结果集拖垮只读适配器）；非整数 fail-closed。
    """
    name = _dialectName(dialect)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValidationError(f"limit 必须是整数，收到 {limit!r}")
    bounded = max(1, min(limit, _MAX_LIMIT))
    return f"LIMIT {bounded}" if name == "mysql" else f"FETCH FIRST {bounded} ROWS ONLY"


def _windowPredicate(order: ResolvedBinding, window: DeliveryTrackingWindow) -> str:
    """窗口谓词：下单日期落在 [dateFrom, dateTo) —— 左闭右开。"""
    orderDate = order.column("ORDER_DATE")
    return (
        f"{orderDate} >= DATE '{window.dateFrom.isoformat()}'"
        f" AND {orderDate} < DATE '{window.dateTo.isoformat()}'"
    )


def _receiptAggregate(receipt: ResolvedBinding) -> str:
    """收货子查询：按 PO_NO+PO_LINE_NO 聚合已收量（含物料名与最后收货日）。"""
    poNo = receipt.column("PO_NO")
    poLine = receipt.column("PO_LINE_NO")
    qty = receipt.column("RCV_QTY_STU")
    itemDesc = receipt.column("ITEM_DESC")
    rcvDate = receipt.column("RCV_DATE")
    return (
        f"(SELECT {poNo} AS PO_NO, {poLine} AS PO_LINE_NO, "
        f"SUM({qty}) AS RECEIVED_QTY, "
        f"MAX({itemDesc}) AS ITEM_DESC, MAX({rcvDate}) AS RECEIPT_DATE "
        f"FROM {receipt.qualifiedTable()} "
        f"GROUP BY {poNo}, {poLine})"
    )


def _inClause(alias: str, column: str, keys: Iterable[str]) -> str | None:
    """`alias.column IN ('a','b')`；无有效键 ⇒ None（不带空 IN）。"""
    values = [str(key) for key in keys if str(key)]
    if not values:
        return None
    literals = ",".join("'" + _esc(value) + "'" for value in values)
    return f"{alias}.{column} IN ({literals})"


def _andClauses(*clauses: str | None) -> str:
    """把可选谓词拼成 ` AND ...` 串（None/空串跳过）。"""
    return "".join(f" AND {clause}" for clause in clauses if clause)


# ---------------------------------------------------------------- SQL 模板


def buildDeliveryTrackingSummarySql(
    *,
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    window: DeliveryTrackingWindow,
    dialect: str = "oracle",
) -> str:
    """窗口级汇总：总行数 / 订单数 / 订购量 / 已收量 / 订单三态。

    行级先按 PO_NO 聚合，再在订单级判三态 ⇒ 「全到货」不会被
    「部分行已到」误判。别名是 Task 4 `_assembleSummary` 的取值契约。
    """
    _dialectName(dialect)
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    orderQty = order.column("PO_QTY")
    return f"""
SELECT
  SUM(T.LINE_COUNT) AS TOTAL_LINES,
  COUNT(*) AS TOTAL_ORDERS,
  SUM(T.ORDER_QTY) AS ORDERED_QTY,
  SUM(T.RECEIVED_QTY) AS RECEIVED_QTY,
  SUM(CASE WHEN T.OPEN_LINES = 0 THEN 1 ELSE 0 END) AS ALL_ARRIVED,
  SUM(CASE WHEN T.OPEN_LINES > 0 AND T.RECEIVED_LINES > 0 THEN 1 ELSE 0 END) AS PARTIAL,
  SUM(CASE WHEN T.RECEIVED_LINES = 0 THEN 1 ELSE 0 END) AS NOT_ARRIVED
FROM (
  SELECT O.{poNo} AS PO_NO,
         COUNT(*) AS LINE_COUNT,
         SUM(O.{orderQty}) AS ORDER_QTY,
         SUM(COALESCE(R.RECEIVED_QTY, 0)) AS RECEIVED_QTY,
         SUM(CASE WHEN O.{orderQty} - COALESCE(R.RECEIVED_QTY, 0) > 0
                  THEN 1 ELSE 0 END) AS OPEN_LINES,
         SUM(CASE WHEN COALESCE(R.RECEIVED_QTY, 0) > 0
                  THEN 1 ELSE 0 END) AS RECEIVED_LINES
  FROM {order.qualifiedTable()} O
  LEFT JOIN {_receiptAggregate(receipt)} R
    ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
  WHERE {_windowPredicate(order, window)}
  GROUP BY O.{poNo}
) T""".strip()


def buildDeliveryTrackingBySupplierSql(
    *,
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    window: DeliveryTrackingWindow,
    supplierKeys: tuple[str, ...] = (),
    limit: int = 50,
    dialect: str = "oracle",
) -> str:
    """供应商分组：每家的订单数 / 三态 / 到货量（按未到货订单数降序）。"""
    supplierCode = order.column("SUPPLIER_CODE")
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    orderQty = order.column("PO_QTY")
    supplierFilter = _andClauses(_inClause("O", supplierCode, supplierKeys))
    tail = _rowLimit(limit, dialect)
    return f"""
SELECT
  S.SUPPLIER_CODE AS SUPPLIER_CODE,
  COUNT(*) AS TOTAL_ORDERS,
  SUM(CASE WHEN S.OPEN_LINES = 0 THEN 1 ELSE 0 END) AS ALL_ARRIVED,
  SUM(CASE WHEN S.OPEN_LINES > 0 AND S.RECEIVED_LINES > 0 THEN 1 ELSE 0 END) AS PARTIAL,
  SUM(CASE WHEN S.RECEIVED_LINES = 0 THEN 1 ELSE 0 END) AS NOT_ARRIVED,
  SUM(S.ORDERED_QTY) AS ORDERED_QTY,
  SUM(S.RECEIVED_QTY) AS RECEIVED_QTY
FROM (
  SELECT O.{supplierCode} AS SUPPLIER_CODE, O.{poNo} AS PO_NO,
         SUM(O.{orderQty}) AS ORDERED_QTY,
         SUM(COALESCE(R.RECEIVED_QTY, 0)) AS RECEIVED_QTY,
         SUM(CASE WHEN O.{orderQty} - COALESCE(R.RECEIVED_QTY, 0) > 0
                  THEN 1 ELSE 0 END) AS OPEN_LINES,
         SUM(CASE WHEN COALESCE(R.RECEIVED_QTY, 0) > 0
                  THEN 1 ELSE 0 END) AS RECEIVED_LINES
  FROM {order.qualifiedTable()} O
  LEFT JOIN {_receiptAggregate(receipt)} R
    ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
  WHERE {_windowPredicate(order, window)}{supplierFilter}
  GROUP BY O.{supplierCode}, O.{poNo}
) S
GROUP BY S.SUPPLIER_CODE
ORDER BY NOT_ARRIVED DESC
{tail}""".strip()


def buildDeliveryTrackingByOrderSql(
    *,
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    window: DeliveryTrackingWindow,
    supplierKeys: tuple[str, ...] = (),
    orderKeys: tuple[str, ...] = (),
    limit: int = 50,
    dialect: str = "oracle",
) -> str:
    """订单明细：订单号 + 行级（物料 / 订购 / 已收 / 欠交 / 预期与最后收货日）。"""
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    orderQty = order.column("PO_QTY")
    supplierCode = order.column("SUPPLIER_CODE")
    itemCode = order.column("ITEM_CODE")
    expectedDate = order.column("EXP_RCV_DATE")
    extraFilter = _andClauses(
        _inClause("O", supplierCode, supplierKeys),
        _inClause("O", poNo, orderKeys),
    )
    tail = _rowLimit(limit, dialect)
    return f"""
SELECT
  O.{poNo} AS PO_NO,
  O.{poLine} AS PO_LINE_NO,
  O.{supplierCode} AS SUPPLIER_CODE,
  O.{itemCode} AS MATERIAL_CODE,
  R.ITEM_DESC AS MATERIAL_DESC,
  O.{orderQty} AS ORDER_QTY,
  COALESCE(R.RECEIVED_QTY, 0) AS RECEIVED_QTY,
  (O.{orderQty} - COALESCE(R.RECEIVED_QTY, 0)) AS OPEN_QTY,
  O.{expectedDate} AS EXPECTED_DATE,
  R.RECEIPT_DATE AS RECEIPT_DATE
FROM {order.qualifiedTable()} O
LEFT JOIN {_receiptAggregate(receipt)} R
  ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
WHERE {_windowPredicate(order, window)}{extraFilter}
ORDER BY OPEN_QTY DESC
{tail}""".strip()
