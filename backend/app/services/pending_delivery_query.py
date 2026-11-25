"""待到货 / 逾期未到（`pending_delivery_query`，2026-11-25 迁本体 + THBI）。

口径（与到货跟踪同一套，spec 4.1 / 4.2）
----------------------------------------
    已收量（行级）  RECEIVED = SUM(<RCV_QTY_STU>)   -- 收货类按 PO_NO+PO_LINE_NO 聚合
    行级欠交量      OPEN_QTY = <PO_QTY> - RECEIVED
    逾期天数        今天 - <EXP_RCV_DATE>（> 0 即逾期）
    脏数据          行级 OPEN_QTY >= 1e6 ⇒ 源侧录入错误行：统计里剔除，
                    只在 EXCLUDED_ROWS 显式计数（明细仍保留供人核查）

硬规则
------
- 一切对象出自 THBI（`datasource_id=1` + `schema_name=THBI`），由绑定解析保证；
  `datasourceId` **不是**工具参数（调用方给了即 fail-closed）；
- 物理表名 / 列名只来自本体绑定解析（`ResolvedBinding`），**不进代码常量**；
- 供应商名 / 物料名走 MDM（`entity_mapping`），**不 JOIN 数仓字典**；
- 失败一律显式抛出，不静默回退（绑定缺失 `BindingResolutionError`，
  参数/名称无法解析 `ValidationError`）。

本模块从 `agent_tools_procurement` 拆出（文件规模治理）：SQL builder 是纯函数，
handler 是装配层；`agent_tools_procurement` 保留同名 re-export，既有导入路径不变。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.services.agent_tool_types import AgentToolContext, ToolResult
from app.services.ontology_binding_resolver import ResolvedBinding, resolveBinding
from app.services.procurement_query_utils import (
    MAX_LIMIT,
    MONTHS_BACK_MAX,
    MONTHS_BACK_MIN,
    SqlDialect,
    colValue,
    escapeLiteral,
    minusMonths,
)

ORDER_CLASS_NAME = "DWD_PURCHASE_ORDER_DTL"
RECEIPT_CLASS_NAME = "DWD_GOODS_RECEIPT_DTL"
FACILITY_CLASS_NAME = "DIM_FACILITY"

#: 绑定必需属性（与四段 SQL 的取值一一对应；缺一即 fail-closed）。
_ORDER_REQUIRED_PROPERTIES = (
    "PO_NO",
    "PO_LINE_NO",
    "PO_QTY",
    "ORDER_DATE",
    "SUPPLIER_CODE",
    "PUR_SITE_CODE",
    "EXP_RCV_DATE",
)
_RECEIPT_REQUIRED_PROPERTIES = ("PO_NO", "PO_LINE_NO", "RCV_QTY_STU")
_FACILITY_REQUIRED_PROPERTIES = ("FCY_0", "FCYNAM_0")

#: 健全性阈值：欠交量 ≥ 1e6 视为源侧脏数据（探针实测 43 行「数量=物料码」），
#: 剔除但**显式回报**（excludedRows），不静默丢。
_OPEN_QTY_SANITY_MAX = 1_000_000

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CJK_RE = re.compile(r"[一-鿿]")

#: 待到货查询的同日结果缓存：key = 全部过滤参数 + 数据源 + 绑定签名 + 当天日期，
#: TTL = 距当日午夜（ODS 每日 ETL ⇒ 同日数据视为不变，2026-11-24 需求）。
#: 进程内缓存：重启即失效（保守侧），跨天由 key 中的日期自然隔离。
_PENDING_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def validateDateArg(value: str | None, field: str) -> str | None:
    """YYYY-MM-DD 参数校验（格式/日历非法 fail-closed）。"""
    if value is None:
        return None
    if not _DATE_RE.match(value):
        raise ValidationError(f"{field} 日期格式应为 YYYY-MM-DD，收到 {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是有效日期：{value!r}") from exc
    return value


@dataclass(frozen=True)
class PendingDeliveryFilters:
    """待到货查询过滤（2026-11-24：多选供应商/物料/基地 + 日期段）。

    - 绑定随过滤器一起传递：SQL builder 是纯函数，物理表列只从 `ResolvedBinding`
      取，不在模块里留任何表名/列名常量；
    - `materialKeys`：**必须是已解析的物料编码**（中文名由 handler 先经 MDM
      解析成 `enterprise_code`）；`siteKeys` 每项既匹配编码也匹配基地名
      （基地名走 `DIM_FACILITY` 的 `FCYNAM_0` 模糊）；
    - `dateFrom/dateTo`（YYYY-MM-DD）：给出即**覆盖** monthsBack；
      非法格式 fail-closed 抛 ValidationError，不静默忽略。
    """

    orderBinding: ResolvedBinding
    receiptBinding: ResolvedBinding
    facilityBinding: ResolvedBinding | None = None
    supplierKeys: tuple[str, ...] = ()
    materialKeys: tuple[str, ...] = ()
    siteKeys: tuple[str, ...] = ()
    dateFrom: str | None = None
    dateTo: str | None = None

    def __post_init__(self) -> None:
        validateDateArg(self.dateFrom, "dateFrom")
        validateDateArg(self.dateTo, "dateTo")

    @staticmethod
    def _splitKeys(keys: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """按「含中文 ⇒ 名称」分类：编码走 IN，名称走字典表 LIKE。"""
        codes = tuple(k for k in keys if not _CJK_RE.search(k))
        names = tuple(k for k in keys if _CJK_RE.search(k))
        return codes, names

    def _siteNameKeys(self) -> tuple[str, ...]:
        """基地名称项（需要 JOIN `DIM_FACILITY`）。"""
        return tuple(k for k in self.siteKeys if _CJK_RE.search(k))

    def windowSql(self, monthsBack: int, today: date) -> str:
        """时间窗：显式区间优先，否则近 N 月（闭区间，沿用既有行为）。"""
        orderDate = f"O.{self.orderBinding.column('ORDER_DATE')}"
        if self.dateFrom and self.dateTo:
            return f"{orderDate} >= DATE '{self.dateFrom}' AND {orderDate} <= DATE '{self.dateTo}'"
        since = minusMonths(
            today, max(MONTHS_BACK_MIN, min(monthsBack, MONTHS_BACK_MAX))
        )
        return f"{orderDate} >= DATE '{since.isoformat()}'"

    def joinSql(self) -> str:
        """基地名过滤需要的 `DIM_FACILITY` JOIN（无名称项 ⇒ 不 JOIN）。"""
        if not self._siteNameKeys():
            return ""
        facility = self.facilityBinding
        if facility is None:
            raise ValidationError(
                "siteKeys 含基地名称，但缺少 DIM_FACILITY 本体绑定"
            )
        orderSite = self.orderBinding.column("PUR_SITE_CODE")
        return (
            f" JOIN {facility.qualifiedTable()} f"
            f" ON f.{facility.column('FCY_0')} = O.{orderSite}"
        )

    def filterSql(self) -> str:
        """过滤条件：供应商编码 / 物料编码 / 基地编码与名称。

        物料名称必须已在 handler 侧经 MDM 解析成编码；残留名称项 fail-closed
        （旧实现靠数仓字典 LIKE 兜底，迁 THBI 后字典不在数仓层）。
        """
        order = self.orderBinding
        conds: list[str] = []
        if self.supplierKeys:
            conds.append(
                f"O.{order.column('SUPPLIER_CODE')} IN "
                f"({_literalList(self.supplierKeys)})"
            )
        if self.materialKeys:
            codes, names = self._splitKeys(self.materialKeys)
            if names:
                raise ValidationError(
                    f"materialKeys 含未解析的物料名称：{list(names)}"
                    "（请先经 MDM 解析为物料编码）"
                )
            conds.append(
                f"O.{order.column('ITEM_CODE')} IN ({_literalList(codes)})"
            )
        if self.siteKeys:
            codes, names = self._splitKeys(self.siteKeys)
            parts: list[str] = []
            if codes:
                parts.append(
                    f"O.{order.column('PUR_SITE_CODE')} IN ({_literalList(codes)})"
                )
            if names:
                facility = self.facilityBinding
                if facility is None:
                    raise ValidationError(
                        "siteKeys 含基地名称，但缺少 DIM_FACILITY 本体绑定"
                    )
                nameColumn = facility.column("FCYNAM_0")
                parts.extend(
                    f"f.{nameColumn} LIKE '%{escapeLiteral(k)}%'" for k in names
                )
            conds.append("(" + " OR ".join(parts) + ")")
        return (" AND " + " AND ".join(conds)) if conds else ""


# ---------------------------------------------------------------- 绑定 / 名称


async def resolvePendingDeliveryBindings(
    session: AsyncSession,
) -> tuple[ResolvedBinding, ResolvedBinding, ResolvedBinding]:
    """解析订单行 / 收货行 / 基地三条本体绑定（缺一即 fail-closed）。"""
    order = await resolveBinding(
        session,
        className=ORDER_CLASS_NAME,
        requiredProperties=_ORDER_REQUIRED_PROPERTIES,
    )
    receipt = await resolveBinding(
        session,
        className=RECEIPT_CLASS_NAME,
        requiredProperties=_RECEIPT_REQUIRED_PROPERTIES,
    )
    facility = await resolveBinding(
        session,
        className=FACILITY_CLASS_NAME,
        requiredProperties=_FACILITY_REQUIRED_PROPERTIES,
    )
    return order, receipt, facility


async def resolveMaterialKeys(
    session: AsyncSession, terms: tuple[str, ...]
) -> tuple[str, ...]:
    """物料项 → 编码列表：编码原样保留，中文名经 MDM 解析成 `enterprise_code`。

    名称在 MDM 无任何命中 ⇒ `ValidationError`（fail-closed）：静默丢掉一个
    用户显式给的过滤条件，会把「查指定物料」悄悄变成「查全部物料」。
    """
    if not terms:
        return ()
    from app.services.entity_mapping_service import EntityMappingService

    codes, names = PendingDeliveryFilters._splitKeys(terms)
    resolved: list[str] = list(codes)
    for term in names:
        hits = await EntityMappingService().searchMappings(
            session, q=term, entityType="MATERIAL", limit=MAX_LIMIT
        )
        termCodes = [
            str(getattr(row, "enterprise_code", "") or "")
            for row in hits
            if getattr(row, "enterprise_code", None)
        ]
        if not termCodes:
            raise ValidationError(f"物料「{term}」在 MDM 中没有匹配编码")
        for code in termCodes:
            if code not in resolved:
                resolved.append(code)
    return tuple(resolved)


async def loadMdmNames(
    session: AsyncSession, *, entityType: str, codes: tuple[str, ...]
) -> dict[str, str]:
    """按业务码批量取 MDM 名称（供应商 / 物料），只认 `source_system = MDM`。

    名称**不 JOIN 数仓字典**（覆盖率不足）；MDM 无行 ⇒ 名称缺省（UI 显示编码）。
    """
    if not codes:
        return {}
    from app.domain.enums import SourceSystem
    from app.domain.models import EntityMapping

    rows = (
        await session.execute(
            select(EntityMapping.enterprise_code, EntityMapping.name)
            .where(
                EntityMapping.entity_type == entityType,
                EntityMapping.source_system == SourceSystem.MDM,
                EntityMapping.enterprise_code.in_(list(codes)),
            )
            .order_by(EntityMapping.name.asc().nulls_last())
        )
    ).all()
    names: dict[str, str] = {}
    for enterpriseCode, name in rows:
        code = str(enterpriseCode)
        if code not in names and name:
            names[code] = name
    return names


def _withNames(
    row: dict[str, Any],
    *,
    supplierNames: dict[str, str],
    materialNames: dict[str, str],
) -> dict[str, Any]:
    """给 SQL 行补 MDM 名称（返回新 dict，不原地改）。"""
    enriched = dict(row)
    supplierCode = colValue(row, "SUPPLIER_CODE")
    if supplierCode is not None:
        enriched["SUPPLIER_NAME"] = supplierNames.get(str(supplierCode))
    materialCode = colValue(row, "ITEM_CODE")
    if materialCode is not None:
        enriched["MATERIAL_DESC"] = materialNames.get(str(materialCode))
    return enriched


# ---------------------------------------------------------------- SQL 片段


def _literalList(values: tuple[str, ...]) -> str:
    """`'a', 'b'` 字面量列表（调用方已保证非空）。"""
    return ", ".join(f"'{escapeLiteral(value)}'" for value in values)


def _receiptAggregate(receipt: ResolvedBinding) -> str:
    """收货子查询：按 PO_NO+PO_LINE_NO 聚合已收量（与到货跟踪同口径）。"""
    poNo = receipt.column("PO_NO")
    poLine = receipt.column("PO_LINE_NO")
    qty = receipt.column("RCV_QTY_STU")
    return (
        f"(SELECT {poNo} AS PO_NO, {poLine} AS PO_LINE_NO, "
        f"SUM({qty}) AS RECEIVED_QTY "
        f"FROM {receipt.qualifiedTable()} "
        f"GROUP BY {poNo}, {poLine})"
    )


def _openQty(order: ResolvedBinding) -> str:
    """行级欠交量：`PO_QTY - 收货聚合已收量`（COALESCE 兜未收货）。"""
    return f"O.{order.column('PO_QTY')} - COALESCE(R.RECEIVED_QTY, 0)"


def _receiptJoin(order: ResolvedBinding, receipt: ResolvedBinding) -> str:
    """订单行 LEFT JOIN 收货聚合（按 PO_NO + PO_LINE_NO）。"""
    return (
        f" LEFT JOIN {_receiptAggregate(receipt)} R"
        f" ON R.PO_NO = O.{order.column('PO_NO')}"
        f" AND R.PO_LINE_NO = O.{order.column('PO_LINE_NO')}"
    )


def _openQtyCond(order: ResolvedBinding) -> str:
    """欠交为正且未超脏数据阈值（统计 / 明细 / 计数共用同一口径）。"""
    return (
        f"{_openQty(order)} > 0"
        f" AND {_openQty(order)} < {_OPEN_QTY_SANITY_MAX}"
    )


# ---------------------------------------------------------------- SQL 模板


def buildPendingDeliverySummarySql(
    *,
    monthsBack: int,
    today: date,
    filters: PendingDeliveryFilters,
    dialect: str = "oracle",
) -> str:
    """欠交汇总（单语句）：行数 / 总欠交量 / 逾期行数 / 脏数据行数。

    派生表别名 ``) t``：MySQL 必须有（错误 1248），裸别名（无 AS）Oracle 也合法。
    """
    order = filters.orderBinding
    receipt = filters.receiptBinding
    d = SqlDialect(dialect)
    todayLit = f"DATE '{today.isoformat()}'"
    overdue = d.overdueDays(
        todayLit, "O." + order.column("EXP_RCV_DATE")
    )
    return f"""
SELECT
  SUM(CASE WHEN OPEN_QTY > 0 AND OPEN_QTY < {_OPEN_QTY_SANITY_MAX} THEN 1 ELSE 0 END) AS OPEN_LINES,
  {d.coalesce(f"SUM(CASE WHEN OPEN_QTY > 0 AND OPEN_QTY < {_OPEN_QTY_SANITY_MAX} THEN OPEN_QTY ELSE 0 END)", "0")} AS OPEN_QTY,
  SUM(CASE WHEN OPEN_QTY > 0 AND OPEN_QTY < {_OPEN_QTY_SANITY_MAX} AND OVERDUE_DAYS > 0 THEN 1 ELSE 0 END) AS OVERDUE_LINES,
  SUM(CASE WHEN OPEN_QTY >= {_OPEN_QTY_SANITY_MAX} THEN 1 ELSE 0 END) AS EXCLUDED_ROWS
FROM (
  SELECT O.{order.column("PO_NO")} AS PO_NO,
         O.{order.column("PO_LINE_NO")} AS PO_LINE_NO,
         {_openQty(order)} AS OPEN_QTY,
         {overdue} AS OVERDUE_DAYS
  FROM {order.qualifiedTable()} O{filters.joinSql()}{_receiptJoin(order, receipt)}
  WHERE {filters.windowSql(monthsBack, today)}{filters.filterSql()}
) t""".strip()


def buildPendingDeliveryBySupplierSql(
    *,
    monthsBack: int,
    today: date,
    limit: int,
    filters: PendingDeliveryFilters,
    dialect: str = "oracle",
) -> str:
    """按供应商聚合欠交（催货清单的分组视角）。方言语义同 summary。

    供应商名称由装配层从 MDM 补（SQL 只出编码）⇒ 不 JOIN 数仓字典。
    """
    order = filters.orderBinding
    receipt = filters.receiptBinding
    d = SqlDialect(dialect)
    todayLit = f"DATE '{today.isoformat()}'"
    bounded = max(1, min(limit, MAX_LIMIT))
    overdue = d.overdueDays(todayLit, "O." + order.column("EXP_RCV_DATE"))
    return f"""
SELECT
  SUPPLIER_CODE,
  COUNT(*) AS OPEN_LINES,
  SUM(OPEN_QTY) AS OPEN_QTY,
  SUM(CASE WHEN OVERDUE_DAYS > 0 THEN 1 ELSE 0 END) AS OVERDUE_LINES
FROM (
  SELECT O.{order.column("SUPPLIER_CODE")} AS SUPPLIER_CODE,
         {_openQty(order)} AS OPEN_QTY,
         {overdue} AS OVERDUE_DAYS
  FROM {order.qualifiedTable()} O{filters.joinSql()}{_receiptJoin(order, receipt)}
  WHERE {filters.windowSql(monthsBack, today)}
    AND {_openQtyCond(order)}{filters.filterSql()}
) t
GROUP BY SUPPLIER_CODE
ORDER BY OPEN_QTY DESC
{d.tail(bounded)}""".strip()


def buildPendingDeliveryLinesSql(
    *,
    monthsBack: int,
    today: date,
    filters: PendingDeliveryFilters,
    minOverdueDays: int | None,
    limit: int,
    offset: int = 0,
    dialect: str = "oracle",
) -> str:
    """TOP 欠交订单行（明细下钻）：多选供应商/物料/基地 + 日期段 + 逾期下限。"""
    order = filters.orderBinding
    receipt = filters.receiptBinding
    d = SqlDialect(dialect)
    todayLit = f"DATE '{today.isoformat()}'"
    bounded = max(1, min(limit, MAX_LIMIT))
    boundedOffset = max(0, int(offset))
    overdue = d.overdueDays(todayLit, "O." + order.column("EXP_RCV_DATE"))
    conds = [filters.windowSql(monthsBack, today), _openQtyCond(order)]
    if minOverdueDays:
        conds.append(f"{overdue} >= {int(minOverdueDays)}")
    where = " AND ".join(conds) + filters.filterSql()
    return f"""
SELECT O.{order.column("PO_NO")} AS PO_NO,
       O.{order.column("PO_LINE_NO")} AS PO_LINE_NO,
       O.{order.column("ITEM_CODE")} AS ITEM_CODE,
       O.{order.column("SUPPLIER_CODE")} AS SUPPLIER_CODE,
       O.{order.column("PO_QTY")} AS ORDERED_QTY,
       COALESCE(R.RECEIVED_QTY, 0) AS RECEIVED_QTY,
       {_openQty(order)} AS OPEN_QTY,
       O.{order.column("EXP_RCV_DATE")} AS EXPECTED_DATE,
       {overdue} AS OVERDUE_DAYS
FROM {order.qualifiedTable()} O{filters.joinSql()}{_receiptJoin(order, receipt)}
WHERE {where}
ORDER BY OPEN_QTY DESC
{d.tail(bounded, boundedOffset)}""".strip()


def buildPendingDeliveryCountSql(
    *,
    monthsBack: int,
    today: date,
    filters: PendingDeliveryFilters,
    dialect: str = "oracle",
) -> str:
    """欠交行总数（明细页分页用）——与 lines SQL 同口径（含脏数据阈值）。"""
    order = filters.orderBinding
    receipt = filters.receiptBinding
    where = " AND ".join(
        [filters.windowSql(monthsBack, today), _openQtyCond(order)]
    ) + filters.filterSql()
    return f"""
SELECT COUNT(*) AS TOTAL_ROWS
FROM {order.qualifiedTable()} O{filters.joinSql()}{_receiptJoin(order, receipt)}
WHERE {where}""".strip()


# ---------------------------------------------------------------- 结果缓存 / 装配


def _bindingSignature(filters: PendingDeliveryFilters) -> str:
    """绑定签名：同参数但绑定被换过 ⇒ 不能复用旧 SQL 的结果。"""
    parts = []
    for binding in (filters.orderBinding, filters.receiptBinding, filters.facilityBinding):
        if binding is None:
            parts.append("")
            continue
        columns = ",".join(f"{k}={v}" for k, v in sorted(binding.columns.items()))
        parts.append(f"{binding.qualifiedTable()}[{columns}]")
    return "|".join(parts)


def _pendingCacheKey(
    *,
    datasourceId: int,
    monthsBack: int,
    filters: PendingDeliveryFilters,
    minOverdueDays: int | None,
    today: date,
) -> str:
    payload = {
        "ds": datasourceId,
        "months": monthsBack,
        "from": filters.dateFrom,
        "to": filters.dateTo,
        "suppliers": sorted(filters.supplierKeys),
        "materials": sorted(filters.materialKeys),
        "sites": sorted(filters.siteKeys),
        "overdue": minOverdueDays,
        "day": today.isoformat(),
        "binding": _bindingSignature(filters),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cacheTtlSeconds(now: datetime) -> int:
    """TTL = 距当日午夜（同日复用，跨天自然失效）。"""
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min)
    return max(60, int((midnight - now).total_seconds()))


async def _loadNamesForRows(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> tuple[dict[str, str], dict[str, str]]:
    """收集行里的供应商/物料编码并批量取 MDM 名称。"""
    supplierCodes = {
        str(code)
        for code in (colValue(row, "SUPPLIER_CODE") for row in rows)
        if code is not None
    }
    materialCodes = {
        str(code)
        for code in (colValue(row, "ITEM_CODE") for row in rows)
        if code is not None
    }
    supplierNames = await loadMdmNames(
        session, entityType="SUPPLIER", codes=tuple(sorted(supplierCodes))
    )
    materialNames = await loadMdmNames(
        session, entityType="MATERIAL", codes=tuple(sorted(materialCodes))
    )
    return supplierNames, materialNames


async def enrichPendingLineRows(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """明细行补 MDM 名称（API 明细页复用；返回新 dict，不原地改）。"""
    supplierNames, materialNames = await _loadNamesForRows(session, rows)
    return [
        _withNames(row, supplierNames=supplierNames, materialNames=materialNames)
        for row in rows
    ]


async def _loadPendingDeliveryData(
    session: AsyncSession,
    adapter: Any,
    *,
    datasourceId: int,
    monthsBack: int,
    today: date,
    filters: PendingDeliveryFilters,
    minOverdueDays: int | None,
    limit: int,
) -> dict[str, Any]:
    """四段查询（汇总/按供应商/明细）+ 同日结果缓存。

    命中 ⇒ 不再查库（省 DB 与工具执行成本）；未命中 ⇒ 查询 → MDM 补名 → 写缓存。
    """
    cacheKey = _pendingCacheKey(
        datasourceId=datasourceId,
        monthsBack=monthsBack,
        filters=filters,
        minOverdueDays=minOverdueDays,
        today=today,
    )
    hit = _PENDING_RESULT_CACHE.get(cacheKey)
    now = datetime.now()
    if hit is not None and hit[0] > now.timestamp():
        return hit[1]

    dialect = SqlDialect.fromAdapter(adapter).name
    summaryRows = await adapter.execute_read_only(
        buildPendingDeliverySummarySql(
            monthsBack=monthsBack, today=today, filters=filters, dialect=dialect
        )
    )
    supplierRows = await adapter.execute_read_only(
        buildPendingDeliveryBySupplierSql(
            monthsBack=monthsBack, today=today, limit=5, filters=filters,
            dialect=dialect,
        )
    )
    lineRows = await adapter.execute_read_only(
        buildPendingDeliveryLinesSql(
            monthsBack=monthsBack, today=today, filters=filters,
            minOverdueDays=minOverdueDays, limit=limit, dialect=dialect,
        )
    )

    summaryRow = summaryRows[0] if summaryRows else {}
    supplierNames, materialNames = await _loadNamesForRows(
        session, [*supplierRows, *lineRows]
    )
    enrichedSupplier = [
        _withNames(row, supplierNames=supplierNames, materialNames={})
        for row in supplierRows
    ]
    enrichedLines = [
        _withNames(row, supplierNames=supplierNames, materialNames=materialNames)
        for row in lineRows
    ]
    data: dict[str, Any] = {
        "summary": {
            "openLines": int(colValue(summaryRow, "OPEN_LINES") or 0),
            "openQty": colValue(summaryRow, "OPEN_QTY"),
            "overdueLines": int(colValue(summaryRow, "OVERDUE_LINES") or 0),
            "excludedRows": int(colValue(summaryRow, "EXCLUDED_ROWS") or 0),
        },
        "bySupplier": [
            {
                "supplierCode": colValue(row, "SUPPLIER_CODE"),
                "supplierName": colValue(row, "SUPPLIER_NAME"),
                "openLines": int(colValue(row, "OPEN_LINES") or 0),
                "openQty": colValue(row, "OPEN_QTY"),
                "overdueLines": int(colValue(row, "OVERDUE_LINES") or 0),
            }
            for row in enrichedSupplier
        ],
        "topLines": [dict(row) for row in enrichedLines],
    }
    _PENDING_RESULT_CACHE[cacheKey] = (_cacheTtlSeconds(now) + now.timestamp(), data)
    return data


def _camelPendingRow(row: dict[str, Any]) -> dict[str, Any]:
    expected = colValue(row, "EXPECTED_DATE")
    return {
        "poNo": colValue(row, "PO_NO"),
        "poLine": colValue(row, "PO_LINE_NO"),
        "materialCode": colValue(row, "ITEM_CODE"),
        "materialDesc": colValue(row, "MATERIAL_DESC"),
        "supplierCode": colValue(row, "SUPPLIER_CODE"),
        "supplierName": colValue(row, "SUPPLIER_NAME"),
        "orderedQty": colValue(row, "ORDERED_QTY"),
        "receivedQty": colValue(row, "RECEIVED_QTY"),
        "openQty": colValue(row, "OPEN_QTY"),
        "expectedDate": str(expected) if expected else None,
        "overdueDays": colValue(row, "OVERDUE_DAYS"),
    }


# ---------------------------------------------------------------- handler 装配


def _adapterFor(dsId: int, ds: Any) -> Any:
    """业务库 adapter 获取（惰性转发到既有实现，保持 monkeypatch 点单一）。"""
    from app.services.agent_tools_procurement import _adapterFor as impl

    return impl(dsId, ds)


async def _loadActiveDatasource(session: AsyncSession, datasourceId: int) -> Any:
    """按 id 取数据源（THBI 固定）；不存在/停用 fail-closed。"""
    from app.services.agent_tools_procurement import (
        _loadActiveDatasource as impl,
    )

    return await impl(session, datasourceId)


def _intArg(value: object, field: str, default: int) -> int:
    """整数参数（bool/字符串 fail-closed，不静默 `int()` 转换）。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数，收到 {value!r}")
    return value


def _optionalInt(value: object, field: str) -> int | None:
    if value is None or value == 0:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数，收到 {value!r}")
    return value


def _strList(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


async def buildPendingDeliveryFilters(
    session: AsyncSession,
    *,
    orderBinding: ResolvedBinding,
    receiptBinding: ResolvedBinding,
    facilityBinding: ResolvedBinding | None,
    supplierKeys: tuple[str, ...] = (),
    materialKeys: tuple[str, ...] = (),
    siteKeys: tuple[str, ...] = (),
    dateFrom: str | None = None,
    dateTo: str | None = None,
) -> PendingDeliveryFilters:
    """过滤参数 → `PendingDeliveryFilters`（物料名先经 MDM 解析成编码）。"""
    resolvedMaterialKeys = await resolveMaterialKeys(session, materialKeys)
    return PendingDeliveryFilters(
        orderBinding=orderBinding,
        receiptBinding=receiptBinding,
        facilityBinding=facilityBinding,
        supplierKeys=supplierKeys,
        materialKeys=resolvedMaterialKeys,
        siteKeys=siteKeys,
        dateFrom=dateFrom,
        dateTo=dateTo,
    )


async def pendingDeliveryHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """`pending_delivery_query`：欠交汇总 + 按供应商 + TOP 明细（四段只读 SQL）。

    数据源固定 THBI（`datasource_id=1`，由绑定解析闸门保证），物理表列全部
    来自本体绑定；调用方给 `datasourceId` 一律拒绝（不是工具参数）。

    脏数据（欠交量 ≥ 1e6 的源侧录入错误）剔除并在 summary.excludedRows 显式回报。
    """
    if "datasourceId" in args:
        raise ValidationError(
            "pending_delivery_query 的数据源固定为 THBI，不接受调用方指定数据源（datasourceId）"
        )
    monthsBack = _intArg(args.get("monthsBack"), "monthsBack", 12)
    minOverdueDays = _optionalInt(args.get("minOverdueDays"), "minOverdueDays")
    limit = _intArg(args.get("limit"), "limit", 20)

    legacySupplier = str(args.get("supplierKey") or "").strip()
    supplierKeys = _strList(args.get("supplierKeys")) or (
        (legacySupplier,) if legacySupplier else ()
    )
    order, receipt, facility = await resolvePendingDeliveryBindings(session)
    filters = await buildPendingDeliveryFilters(
        session,
        orderBinding=order,
        receiptBinding=receipt,
        facilityBinding=facility,
        supplierKeys=supplierKeys,
        materialKeys=_strList(args.get("materialKeys")),
        siteKeys=_strList(args.get("siteKeys")),
        dateFrom=args.get("dateFrom"),
        dateTo=args.get("dateTo"),
    )

    ds = await _loadActiveDatasource(session, order.datasourceId)
    adapter = _adapterFor(order.datasourceId, ds)
    today = date.today()

    data = await _loadPendingDeliveryData(
        session, adapter, datasourceId=order.datasourceId, monthsBack=monthsBack,
        today=today, filters=filters, minOverdueDays=minOverdueDays, limit=limit,
    )
    summary = data["summary"]
    bySupplier = data["bySupplier"]
    lineRows = [_camelPendingRow(row) for row in data["topLines"]]
    openLines = int(summary["openLines"] or 0)
    if openLines == 0:
        return ToolResult(
            data={
                "via": "business_db", "datasourceId": order.datasourceId,
                "summary": summary, "bySupplier": [], "topLines": [],
            },
            answer=f"近 {monthsBack} 个月没有欠交订单行（全部已交清）。",
        )

    topSuppliers = "、".join(
        f"{item['supplierCode']}({item['openQty']})" for item in bySupplier[:3]
    )
    excludedNote = (
        f"；另有 {summary['excludedRows']} 行源侧数量异常已剔除"
        if summary["excludedRows"]
        else ""
    )
    return ToolResult(
        data={
            "via": "business_db", "datasourceId": order.datasourceId,
            "summary": summary, "bySupplier": bySupplier, "topLines": lineRows,
        },
        answer=(
            f"近 {monthsBack} 个月欠交 {openLines} 行、总量 {summary['openQty']}；"
            f"其中逾期 {summary['overdueLines']} 行{excludedNote}。"
            f"欠交 TOP 供应商：{topSuppliers}。"
        ),
    )
