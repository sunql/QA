"""催货单的**服务端行回查**（feat/delivery-chase-closed-loop）。

`POST /chase/drafts/from-pending` 只收 `{poNo, poLine}` 身份对 ⇒ 服务端回查
最新欠交数量/日期/供应商（**不信前端带来的数量**：防篡改、防陈旧）。

**只读红线**：走 `pending_delivery_query` 同款管线（本体绑定解析 + 只读
adapter）—— 绝不在催货链路里造第二条 SQL 路径（两路口径漂移 = 催错账）。

回查 SQL 与 `buildPendingDeliveryLinesSql` 的差异只有 WHERE：
- 不带月份窗口（催的是「历史欠交」，窗口会漏掉逾期很久的行）；
- 带身份对 IN 过滤（最多 200 对，SQL 字面量经 `_sqlLiteral` 转义）。
其余列口径（OPEN_QTY 派生、脏数据剔除、MDM 补名）**逐字一致**。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.services.ontology_binding_resolver import (
    ResolvedBinding,
    resolveBinding,
)
from app.services.pending_delivery_query import (
    ORDER_CLASS_NAME,
    RECEIPT_CLASS_NAME,
    _loadNamesForRows,
    _openQty,
    _receiptJoin,
)
from app.services.pending_delivery_rows import withNames
from app.services.procurement_datasource import adapterFor, loadActiveDatasource
from app.services.procurement_query_utils import SqlDialect
from app.services.schema_introspection_service import SchemaIntrospectionService  # noqa: F401

#: 单次回查的行数上限（与 `MAX_CHASE_LINES` 对齐）
LOOKUP_MAX_LINES = 200


def _sqlLiteral(value: str) -> str:
    """SQL 字面量转义（单引号双写；与 schema_introspection_service._sqlLiteral 同口径）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _colText(row: dict[str, Any], key: str) -> Any:
    """大小写不敏感取列（Oracle 返回大写键 / PG 小写键；与 colValue 同口径）。"""
    for candidate in (key, key.lower(), key.upper()):
        if candidate in row:
            return row[candidate]
    return None


def buildChaseLookupSql(
    *,
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    identities: list[tuple[str, str]],
    dialect: str = "oracle",
) -> str:
    """回查 SQL（纯函数，可单测）。

    WHERE **只有身份对 IN (...)** —— 不含任何 openQty 条件（见调用处注释：
    所有订单行都可催，脏数据行 / 已到货行同样要能催）。
    """
    d = SqlDialect(dialect)
    poNoCol = order.column("PO_NO")
    poLineCol = order.column("PO_LINE_NO")
    pairConds = ", ".join(
        f"({_sqlLiteral(po)}, {_sqlLiteral(line)})" for po, line in sorted(set(identities))
    )
    where = f"(O.{poNoCol}, O.{poLineCol}) IN ({pairConds})"
    receiptJoin = _receiptJoin(order, receipt, windowSql="1=1")
    return f"""
SELECT O.{poNoCol} AS PO_NO,
       O.{poLineCol} AS PO_LINE_NO,
       O.{order.column("ITEM_CODE")} AS ITEM_CODE,
       O.{order.column("SUPPLIER_CODE")} AS SUPPLIER_CODE,
       O.{order.column("PO_QTY")} AS ORDERED_QTY,
       COALESCE(R.RECEIVED_QTY, 0) AS RECEIVED_QTY,
       {_openQty(order)} AS OPEN_QTY,
       O.{order.column("EXP_RCV_DATE")} AS EXPECTED_DATE,
       {d.overdueDays(f"DATE '{date.today().isoformat()}'", "O." + order.column("EXP_RCV_DATE"))} AS OVERDUE_DAYS
FROM {order.qualifiedTable()} O{receiptJoin}
WHERE {where}""".strip()


async def fetchPendingLinesByIdentity(
    session: AsyncSession,
    *,
    identities: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """按 `(poNo, poLine)` 身份对回查欠交行（只读）。

    返回 `((poNo, poLine) → 原始行)`；查不到的身份对**不在结果里**
    （调用方据此点名 `skippedLines`）。
    """
    if not identities:
        return {}
    if len(identities) > LOOKUP_MAX_LINES:
        raise ValidationError(f"一次最多回查 {LOOKUP_MAX_LINES} 行（收到 {len(identities)}）")

    order = await resolveBinding(
        session,
        className=ORDER_CLASS_NAME,
        requiredProperties=("PO_NO", "PO_LINE_NO", "ITEM_CODE", "SUPPLIER_CODE", "PO_QTY", "EXP_RCV_DATE"),
    )
    receipt = await resolveBinding(
        session,
        className=RECEIPT_CLASS_NAME,
        requiredProperties=("PO_NO", "PO_LINE_NO", "RCV_QTY_STU"),
    )
    ds = await loadActiveDatasource(session, order.datasourceId)
    adapter = adapterFor(order.datasourceId, ds)

    sql = buildChaseLookupSql(order=order, receipt=receipt, identities=identities)
    rows = await adapter.execute_read_only(sql)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        poNo = str(_colText(row, "PO_NO"))
        poLine = str(_colText(row, "PO_LINE_NO"))
        result[(poNo, poLine)] = row
    return result


async def fetchPendingLinesEnriched(
    session: AsyncSession, *, identities: list[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """回查 + MDM 补名（供应商名 / 物料名），与待到货明细同口径。"""
    raw = await fetchPendingLinesByIdentity(session, identities=identities)
    if not raw:
        return {}
    supplierNames, materialNames = await _loadNamesForRows(session, list(raw.values()))
    return {
        identity: withNames(
            row,
            supplierNames=supplierNames,
            materialNames=materialNames,
        )
        for identity, row in raw.items()
    }
