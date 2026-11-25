"""到货跟踪三段只读 SQL（2026-11-25，P5 Task 4 拆出）。

窗口/参数解析与 handler 在 `delivery_tracking_query`；本模块只负责
「本体绑定 → 物理表列」的 SQL 渲染（纯函数、无 IO），可独立单测。

脏行（spec 4.4）判据 `OPEN_QTY >= 1e6` 的阈值是本模块与装配层共享的
唯一来源（`_OPEN_QTY_SANITY_MAX`）—— 两处分叉会让 summary 与 byOrder 口径再次漂移。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.domain.exceptions import ValidationError
from app.services.ontology_binding_resolver import ResolvedBinding

if TYPE_CHECKING:
    from app.services.delivery_tracking_query import DeliveryTrackingWindow

_MAX_LIMIT = 200
#: 行级欠交量上限（spec 4.4）：≥ 1e6 判为源侧录入错误的脏行，剔除并显式计数。
#:
#: ⚠ 判据是**欠交量**（`PO_QTY - RECEIVED`），不是**订购量**：大额订购是真实业务
#: （spec 4.2 明确拒绝按订购量剔行）。阈值与既有 `pending_delivery_query` 同源。
_OPEN_QTY_SANITY_MAX = 1_000_000
#: 支持的方言：默认现役 Oracle；场景槽位可切 MySQL 源（与待到货工具一致）。
_SUPPORTED_DIALECTS = frozenset({"oracle", "mysql"})


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
    """`alias.column IN ('a','b')`；无键 ⇒ None（不带空 IN）。

    键必须是非空字符串：`None`/数字/空白串一律 fail-closed（不可信输入），
    绝不静默转成字面量 `'None'`（那会匹配 0 行，把调用错误伪装成“查无数据”）。
    """
    values: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(f"键值必须是非空字符串，收到 {key!r}")
        values.append(key)
    if not values:
        return None
    literals = ",".join("'" + _esc(value) + "'" for value in values)
    return f"{alias}.{column} IN ({literals})"


def _andClauses(*clauses: str | None) -> str:
    """把可选谓词拼成 ` AND ...` 串（None/空串跳过）。"""
    return "".join(f" AND {clause}" for clause in clauses if clause)


def _dirtyCondition(order: ResolvedBinding) -> str:
    """行级 `OPEN_QTY >= 1e6` ⇒ 源侧录入错误条目（spec 4.4）。

    判据用**欠交量**而非订购量：大额订购是真实业务大单（spec 4.2）。
    """
    poQty = order.column("PO_QTY")
    return f"O.{poQty} - COALESCE(R.RECEIVED_QTY, 0) >= {_OPEN_QTY_SANITY_MAX}"


def _erpSupplierNameColumn(order: ResolvedBinding) -> str | None:
    """ERP 供应商名绑定 —— **可选**属性（spec 5.7：MDM 无行时回退 ERP 名）。

    缺该绑定只表示"没有回退名"（不回退、不 fail-closed）；属性名不出代码常量。
    """
    return order.columns.get("SUPPLIER_NAME")


def _erpSupplierNameMeasure(order: ResolvedBinding) -> str:
    """派生表里的 ERP 供应商名聚合（缺绑定 ⇒ 空串，SELECT 少一列）。"""
    column = _erpSupplierNameColumn(order)
    if column is None:
        return ""
    return f",\n         MAX(O.{column}) AS ERP_SUPPLIER_NAME"


def _byOrderErpNameSelect(order: ResolvedBinding) -> str:
    """明细行里的 ERP 名（缺绑定 ⇒ 空串）。"""
    column = _erpSupplierNameColumn(order)
    return "" if column is None else f"\n  O.{column} AS ERP_SUPPLIER_NAME,"


def _lineMeasures(dirty: str, qtyExpr: str, qtyAlias: str) -> str:
    """派生表的六个行级度量（唯一来源，防两个 aggregate 漂移）。

    脏行（`dirty` 为真）在行数/订购量/已收量/欠交行/已收行里一律贡献 0
    （即“既不算欠交也不算已收”），只计入 `DIRTY_LINES`。
    """
    measures = [
        f"SUM(CASE WHEN {dirty} THEN 0 ELSE 1 END) AS LINE_COUNT",
        f"SUM(CASE WHEN {dirty} THEN 0 ELSE {qtyExpr} END) AS {qtyAlias}",
        f"SUM(CASE WHEN {dirty} THEN 0 ELSE COALESCE(R.RECEIVED_QTY, 0) END) AS RECEIVED_QTY",
        f"SUM(CASE WHEN {dirty} THEN 0"
        f" WHEN {qtyExpr} - COALESCE(R.RECEIVED_QTY, 0) > 0"
        f" THEN 1 ELSE 0 END) AS OPEN_LINES",
        f"SUM(CASE WHEN {dirty} THEN 0"
        f" WHEN COALESCE(R.RECEIVED_QTY, 0) > 0"
        f" THEN 1 ELSE 0 END) AS RECEIVED_LINES",
        f"SUM(CASE WHEN {dirty} THEN 1 ELSE 0 END) AS DIRTY_LINES",
    ]
    body = "\n".join(f"         {measure}," for measure in measures[:-1])
    return f"{body}\n         {measures[-1]}"


def _statesSelect(alias: str) -> str:
    """订单三态 select 片段（`alias` 为订单级派生表别名）。

    三态必须**互斥且穷尽**（有效行数 > 0 的订单恰好落入一态）：

    - 全到货：无欠交行（不论是否收过货；订购量 ≤ 0 且未收货也在此态）；
    - 部分：有欠交且有收货；
    - 未到货：有欠交且一行都没收。

    ⚠️ 「未到货」**必须**同时要求 `OPEN_LINES > 0`：否则「订购量 ≤ 0 且未收货」
    的订单会同时命中「全到货」与「未到货」⇒ 三态之和 > 总订单数 ⇒ 到货率 > 100%
    （本项目已知风险点；装配层另有 `DeliveryTrackingInvariantError` 兜底）。
    全脏行订单（`LINE_COUNT = 0`）不落入任一态。
    """
    return (
        f"  SUM(CASE WHEN {alias}.LINE_COUNT > 0 AND {alias}.OPEN_LINES = 0"
        f" THEN 1 ELSE 0 END) AS ALL_ARRIVED,\n"
        f"  SUM(CASE WHEN {alias}.OPEN_LINES > 0 AND {alias}.RECEIVED_LINES > 0"
        f" THEN 1 ELSE 0 END) AS PARTIAL,\n"
        f"  SUM(CASE WHEN {alias}.LINE_COUNT > 0 AND {alias}.RECEIVED_LINES = 0"
        f" AND {alias}.OPEN_LINES > 0 THEN 1 ELSE 0 END) AS NOT_ARRIVED"
    )


def _orderAggregate(order: ResolvedBinding, receipt: ResolvedBinding, window: DeliveryTrackingWindow) -> str:
    """订单级派生表：按 PO_NO 聚合，脏行不计入任何统计但单列 `DIRTY_LINES`。"""
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    qtyExpr = f"O.{order.column('PO_QTY')}"
    return f"""
  SELECT O.{poNo} AS PO_NO,
{_lineMeasures(_dirtyCondition(order), qtyExpr, "ORDER_QTY")}
  FROM {order.qualifiedTable()} O
  LEFT JOIN {_receiptAggregate(receipt)} R
    ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
  WHERE {_windowPredicate(order, window)}
  GROUP BY O.{poNo}"""


def _supplierOrderAggregate(
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    window: DeliveryTrackingWindow,
    supplierFilter: str,
) -> str:
    """供应商 × 订单派生表（脏行口径与 `_orderAggregate` 共用 `_lineMeasures`）。"""
    supplierCode = order.column("SUPPLIER_CODE")
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    qtyExpr = f"O.{order.column('PO_QTY')}"
    return f"""
  SELECT O.{supplierCode} AS SUPPLIER_CODE, O.{poNo} AS PO_NO,
{_lineMeasures(_dirtyCondition(order), qtyExpr, "ORDERED_QTY")}{_erpSupplierNameMeasure(order)}
  FROM {order.qualifiedTable()} O
  LEFT JOIN {_receiptAggregate(receipt)} R
    ON R.PO_NO = O.{poNo} AND R.PO_LINE_NO = O.{poLine}
  WHERE {_windowPredicate(order, window)}{supplierFilter}
  GROUP BY O.{supplierCode}, O.{poNo}"""


# ---------------------------------------------------------------- SQL 模板


def buildDeliveryTrackingSummarySql(
    *,
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    window: DeliveryTrackingWindow,
    dialect: str = "oracle",
) -> str:
    """窗口级汇总：总行数 / 订单数 / 订购量 / 已收量 / 订单三态 / 脏行数。

    行级先按 PO_NO 聚合，再在订单级判三态 ⇒ 「全到货」不会被
    「部分行已到」误判。脏行（欠交量 ≥ 1e6）不计入 `TOTAL_LINES`
    与三态/量级率，只在 `EXCLUDED_ROWS` 显式计数（spec 4.4）。
    别名是 Task 4 `_assembleSummary` 的取值契约。
    """
    _dialectName(dialect)
    return f"""
SELECT
  SUM(CASE WHEN T.LINE_COUNT > 0 THEN T.LINE_COUNT ELSE 0 END) AS TOTAL_LINES,
  SUM(CASE WHEN T.LINE_COUNT > 0 THEN 1 ELSE 0 END) AS TOTAL_ORDERS,
  SUM(T.ORDER_QTY) AS ORDERED_QTY,
  SUM(T.RECEIVED_QTY) AS RECEIVED_QTY,
{_statesSelect("T")},
  SUM(T.DIRTY_LINES) AS EXCLUDED_ROWS
FROM ({_orderAggregate(order, receipt, window)}
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
    """供应商分组：每家的订单数 / 三态 / 到货量 / 脏行数（按未到货订单数降序）。

    脏行口径同 `buildDeliveryTrackingSummarySql`：不计入三态与量级率，
    只在 `EXCLUDED_ROWS` 显式计数（spec 4.4）。
    """
    supplierCode = order.column("SUPPLIER_CODE")
    supplierFilter = _andClauses(_inClause("O", supplierCode, supplierKeys))
    tail = _rowLimit(limit, dialect)
    erpNameSelect = (
        "" if _erpSupplierNameColumn(order) is None else "\n  MAX(S.ERP_SUPPLIER_NAME) AS ERP_SUPPLIER_NAME,"
    )
    return f"""
SELECT
  S.SUPPLIER_CODE AS SUPPLIER_CODE,{erpNameSelect}
  SUM(CASE WHEN S.LINE_COUNT > 0 THEN 1 ELSE 0 END) AS TOTAL_ORDERS,
{_statesSelect("S")},
  SUM(S.ORDERED_QTY) AS ORDERED_QTY,
  SUM(S.RECEIVED_QTY) AS RECEIVED_QTY,
  SUM(S.DIRTY_LINES) AS EXCLUDED_ROWS
FROM ({_supplierOrderAggregate(order, receipt, window, supplierFilter)}
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
    """订单明细：订单号 + 行级（物料 / 订购 / 已收 / 欠交 / 预期与最后收货日）。

    **不剔除脏行**（与 summary / bySupplier 刻意不同）：明细是给人看的证据，
    脏行留着才能被发现；剔除只发生在统计口径里（spec 4.4）。
    """
    poNo = order.column("PO_NO")
    poLine = order.column("PO_LINE_NO")
    orderDate = order.column("ORDER_DATE")
    orderQty = order.column("PO_QTY")
    supplierCode = order.column("SUPPLIER_CODE")
    itemCode = order.column("ITEM_CODE")
    expectedDate = order.column("EXP_RCV_DATE")
    extraFilter = _andClauses(
        _inClause("O", supplierCode, supplierKeys),
        _inClause("O", poNo, orderKeys),
    )
    tail = _rowLimit(limit, dialect)
    erpNameSelect = _byOrderErpNameSelect(order)
    return f"""
SELECT
  O.{poNo} AS PO_NO,
  O.{poLine} AS PO_LINE_NO,
  O.{orderDate} AS ORDER_DATE,
  O.{supplierCode} AS SUPPLIER_CODE,{erpNameSelect}
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
