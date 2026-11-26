"""到货跟踪工具（2026-11-25，P5）：取数窗口 + 三段只读 SQL + handler 装配。

口径（spec 4.1 / 4.2，THBI 实测基线）
------------------------------------
    已收量（行级）  RECEIVED = SUM(<RCV_QTY_STU>)   -- 收货类按 PO_NO+PO_LINE_NO 聚合
    行级欠交量      OPEN_QTY = <PO_QTY> - RECEIVED
    订单三态        全到货 / 部分到货 / 未到货（**互斥且穷尽**）
    订单到货率      全到货订单 / 总订单              （主指标）
    量级率          SUM(RECEIVED) / SUM(<PO_QTY>)    （参考，受超大额未到货行影响）
    脏数据（4.4）   行级 OPEN_QTY >= 1e6 ⇒ 录入错误行：不进三态/量级率，
                   只在 EXCLUDED_ROWS 计数（byOrder 明细保留，供人核查）

窗口（spec 4.3，左闭右开）
--------------------------
- 优先级：显式 dateFrom/dateTo > 24 日锚点 > monthsBack（默认 12）；
- 本期 = **已结束的最近一个完整锚点周期**（今天 2026-11-25 ⇒ 2026-10-24~2026-11-24）；
- 锚点日上限 28（29/30/31 在短月无对应日）。

三段结果（spec 5.3，camelCase）
-------------------------------
    summary     窗口汇总（两个率值 + 三态计数 + excludedRows）
    bySupplier  供应商分组（名称/企业键取自 MDM `entity_mapping`，不 JOIN 数仓字典）
    byOrder     订单 × 行明细（脏行保留，供人核查）

硬规则
------
- 一切对象出自 THBI（`datasource_id=1` + `schema_name=THBI`），由绑定解析保证；
  `datasourceId` **不是**工具参数（调用方给了即 fail-closed）；
- 物理表名/列名只来自本体绑定解析，**不进代码常量**；
- 失败一律显式抛出（非法参数 `ValidationError`，属性缺失 `BindingResolutionError`，
  供应商解析失败 `NotFoundError`，三态不自洽 `DeliveryTrackingInvariantError`），
  不静默纠正、不静默忽略。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.services.agent_tool_types import AgentToolContext, ToolResult
from app.services.delivery_tracking_assembly import (
    # Task 4 契约：装配件与不变式异常从本模块可导入（实现见 assembly 模块）。
    OBSERVATION_BUDGET_CHARS,  # noqa: F401
    DeliveryTrackingInvariantError,  # noqa: F401
    _assembleOrders,
    _assembleSummary,
    _assembleSupplier,
    _orderStatus,  # noqa: F401
    _supplierCodesOf,
    _withSupplierDirectory,
    fitToObservationBudget,
)
from app.services.delivery_tracking_sql import (
    # Task 4 契约：三个 builder 从本模块可导入（实现见 sql 模块）。
    buildDeliveryTrackingByOrderSql,
    buildDeliveryTrackingBySupplierSql,
    buildDeliveryTrackingOrderCountSql,
    buildDeliveryTrackingSummarySql,
)
from app.services.entity_mapping_names import loadMdmDirectory
from app.services.ontology_binding_resolver import ResolvedBinding, resolveBinding
from app.services.procurement_datasource import (
    adapterFor as _getAdapter,
)
from app.services.procurement_datasource import (
    loadActiveDatasource as _loadActiveDataSource,
)
from app.services.procurement_query_utils import (
    SqlDialect,
    colValue,
    monthsBackWindow,
    shiftMonths,
)

ORDER_CLASS_NAME = "DWD_PURCHASE_ORDER_DTL"
RECEIPT_CLASS_NAME = "DWD_GOODS_RECEIPT_DTL"

#: 结果里的数据层可读标识。
#:
#: **事实更正（2026-11-26）**：原值 `thbi_ods_erp` 来自设计期「ODS_ERP 是原始表、
#: DWD 是宽表」的对照，但**本体迁移后两个工具读的是同一批 DWD 宽表**
#: （`THBI.DWD_PURCHASE_ORDER_DTL` + `THBI.DWD_GOODS_RECEIPT_DTL`），
#: 「源层 vs 数仓层」的区分已不存在 ⇒ 角标继续写 ODS_ERP 属**事实错误**。
#: 现按真实物理层写 DWD，与 `agent_tool_config.data_layers`（ACL 层判定）保持一致。
DATA_LAYER_LABEL = "thbi_dwd"
#: 订单明细上限默认值（`_rowLimit` 再把上界夹到 200）。
_DEFAULT_ORDER_LIMIT = 50
#: 绑定必需属性：与三段 SQL 的取值一一对应，缺一即 fail-closed。
#:
#: 收货侧必须含 `RCV_DATE` 与 `ITEM_DESC`：收货聚合 SELECT 会取
#: `MAX(<ITEM_DESC>)` / `MAX(<RCV_DATE>)`（`_receiptAggregate`）。
#:
#: 注意 `SUPPLIER_NAME` **刻意不在必需列表**：它是可选的 ERP 名回退来源
#: （spec 5.7），缺绑定只是没有回退名，不该 fail-closed（SQL builder 按需取）。
_ORDER_REQUIRED_PROPERTIES = (
    "PO_NO",
    "PO_LINE_NO",
    "PO_QTY",
    "ORDER_DATE",
    "SUPPLIER_CODE",
    "ITEM_CODE",
    "EXP_RCV_DATE",
)
_RECEIPT_REQUIRED_PROPERTIES = (
    "PO_NO",
    "PO_LINE_NO",
    "RCV_QTY_STU",
    "ITEM_DESC",
    "RCV_DATE",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: 锚点日上限 28：避免 29/30/31 在短月无对应日（spec 4.3）。
_MAX_ANCHOR_DAY = 28
_MIN_MONTHS_BACK = 1
_MAX_MONTHS_BACK = 36


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


def _anchorDate(today: date, anchorDay: int) -> date:
    """锚点基准日：当月锚点日；若已过（或正值当天），取当月；否则取上月锚点日。

    取「不晚于今天」的最近锚点日 ⇒ 以它为右端点的周期一定已经结束。
    """
    current = date(today.year, today.month, anchorDay)
    return current if current <= today else shiftMonths(current, -1)


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
    if index > 0:
        raise ValidationError(f"periodIndex 不能为正数（未结束的周期不可查询），收到 {periodIndex!r}")
    base = _anchorDate(today, anchor)
    # 本期 = [anchor 上月同日, anchor)；periodIndex=-1 再往前一期。
    return DeliveryTrackingWindow(
        dateFrom=shiftMonths(base, index - 1),
        dateTo=shiftMonths(base, index),
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
    dateFrom, dateTo = monthsBackWindow(today, months)
    return DeliveryTrackingWindow(dateFrom=dateFrom, dateTo=dateTo, mode="monthsBack")


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
        anchorDay 越界、periodIndex 缺 anchorDay 或为正数（未结束周期）、monthsBack 越界。
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


# ---------------------------------------------------------------- 供应商检索


async def _resolveSupplierKey(session: AsyncSession, key: str) -> tuple[int, str, str | None]:
    """供应商检索阶梯（编码 → 企业键 → 全名 → 简称）。

    复用 `Supplier360Service._resolveSupplier`（四段阶梯；简称模糊多命中会
    抛 `NotFoundError` 并列候选，**绝不静默取第一个**）。
    """
    from app.services.supplier_360_service import Supplier360Service

    return await Supplier360Service()._resolveSupplier(session, key)


async def _resolveSupplierCodes(session: AsyncSession, keys: tuple[str, ...]) -> tuple[str, ...]:
    """把用户给的供应商标识解析成业务码列表（去重、保序）。

    解析失败**向上抛**（`ValidationError` ⇒ 422），不静默丢弃该供应商——
    用户给了 3 家、静默丢 1 家，结果就是错的。

    ⚠️ `Supplier360Service._resolveSupplier` 抛的是 `NotFoundError`（404），
    但「过滤词没命中」与未知物料（422）是同一类调用方输入问题；404 最误导
    （看起来像「资源不存在」而不是「这个过滤值查不到」）⇒ 在此统一成 422。
    """
    resolved: list[str] = []
    for key in keys:
        try:
            _, enterpriseCode, _ = await _resolveSupplierKey(session, key)
        except NotFoundError as exc:
            raise ValidationError(
                exc.message, detail=exc.detail, details=exc.details
            ) from exc
        if enterpriseCode not in resolved:
            resolved.append(enterpriseCode)
    return tuple(resolved)


# ---------------------------------------------------------------- handler 装配件


def _keyTuple(value: object, field: str) -> tuple[str, ...]:
    """字符串键列表参数（单个字符串视作一项；数字先 `str()`）。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValidationError(f"{field} 必须是字符串列表，收到 {value!r}")
    keys: list[str] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValidationError(f"{field} 的元素必须是字符串或整数，收到 {item!r}")
        text = str(item).strip()
        if text:
            keys.append(text)
    return tuple(keys)


def _intArg(value: object, field: str, default: int) -> int:
    """整数参数（bool/字符串 fail-closed，不静默 `int()` 转换）。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数，收到 {value!r}")
    return value


async def _loadDeliveryTrackingRows(
    *,
    adapter: Any,
    order: ResolvedBinding,
    receipt: ResolvedBinding,
    window: DeliveryTrackingWindow,
    supplierKeys: tuple[str, ...],
    orderKeys: tuple[str, ...],
    limit: int,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    int,
]:
    """执行四段只读 SQL（summary / bySupplier / byOrder / 订单总数）。

    订单总数用于判定 byOrder 是否被截断（分页单位=订单；sample < total ⇒ 截断）。
    方言由 adapter 推断后显式传给三个 builder（此前从不传 ⇒ mysql 分支死代码）。
    """
    dialect = SqlDialect.fromAdapter(adapter).name
    summaryRows = await adapter.execute_read_only(
        buildDeliveryTrackingSummarySql(
            order=order, receipt=receipt, window=window, dialect=dialect
        )
    )
    supplierRows = await adapter.execute_read_only(
        buildDeliveryTrackingBySupplierSql(
            order=order,
            receipt=receipt,
            window=window,
            supplierKeys=supplierKeys,
            limit=limit,
            dialect=dialect,
        )
    )
    orderRows = await adapter.execute_read_only(
        buildDeliveryTrackingByOrderSql(
            order=order,
            receipt=receipt,
            window=window,
            supplierKeys=supplierKeys,
            orderKeys=orderKeys,
            limit=limit,
            dialect=dialect,
        )
    )
    countRows = await adapter.execute_read_only(
        buildDeliveryTrackingOrderCountSql(
            order=order,
            window=window,
            supplierKeys=supplierKeys,
            orderKeys=orderKeys,
        )
    )
    totalOrders = int(colValue(countRows[0], "TOTAL_ORDERS") or 0) if countRows else 0
    return summaryRows, supplierRows, orderRows, totalOrders


def _resolveTrackedWindow(args: dict) -> DeliveryTrackingWindow:
    """工具参数 → 取数窗口（优先级与校验见 `resolveWindow`）。"""
    return resolveWindow(
        today=date.today(),
        monthsBack=args.get("monthsBack"),
        dateFrom=args.get("dateFrom"),
        dateTo=args.get("dateTo"),
        anchorDay=args.get("anchorDay"),
        periodIndex=args.get("periodIndex"),
    )


async def _resolveFilterArgs(
    session: AsyncSession, args: dict
) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    """过滤参数：供应商（先过检索阶梯）/ 订单号 / 明细上限。"""
    supplierKeys = await _resolveSupplierCodes(session, _keyTuple(args.get("supplierKeys"), "supplierKeys"))
    orderKeys = _keyTuple(args.get("orderKeys"), "orderKeys")
    limit = _intArg(args.get("limit"), "limit", _DEFAULT_ORDER_LIMIT)
    return supplierKeys, orderKeys, limit


async def _resolveTrackedBindings(
    session: AsyncSession,
) -> tuple[ResolvedBinding, ResolvedBinding]:
    """解析订单行 / 收货行两条本体绑定（必需属性缺一即 fail-closed）。"""
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
    return order, receipt


def _buildResultData(
    *,
    datasourceId: int,
    window: DeliveryTrackingWindow,
    summary: dict[str, Any],
    bySupplier: list[dict[str, Any]],
    byOrder: list[dict[str, Any]],
    ordersTruncated: bool,
) -> dict[str, Any]:
    """工具结果 data（spec 5.3 形状；前端结果卡直接消费）。

    `ordersTruncated`：`byOrder` 只是欠交量前 N 单样本（还有更多单）。
    前端**不得**把样本长度当全量计数展示（否则 tab 与 summary 打架）。
    """
    return {
        "via": "business_db",
        "datasourceId": datasourceId,
        "dataLayer": DATA_LAYER_LABEL,
        "window": {
            "dateFrom": window.dateFrom.isoformat(),
            "dateTo": window.dateTo.isoformat(),
            "mode": window.mode,
        },
        "summary": summary,
        "bySupplier": bySupplier,
        "byOrder": byOrder,
        "ordersTruncated": ordersTruncated,
    }


def _fitResultData(
    data: dict[str, Any],
    *,
    totalOrders: int,
) -> dict[str, Any]:
    """按观测字符预算裁剪结果载荷（编排观测上限 50,000，超限 ⇒ 前端解析失败）。

    `ordersTruncated`（样本语义）与预算裁剪后的 `ordersTruncated`（明细被裁）
    **同一个字段**：两者都表示"`byOrder` 不是全量"⇒ 前端必须标注截断，
    不得把样本长度当全量计数展示。
    """
    return fitToObservationBudget(data, totalOrders=totalOrders)


def _buildAnswer(summary: dict[str, Any], window: DeliveryTrackingWindow) -> str:
    """给编排/聊天层的一句话结论（数字与 data 同源）。

    **末尾附业务口径来源句**：编排层把这段 answer 当作「该怎么引用来源」的范本，
    不给范本时 LLM 会照抄观测 JSON 里的内部字段名（工具名 / window.mode 等）
    —— 业务方看不懂（2026-11-26 实测：83 条成功 run 里 68 条把工具名写进正文）。
    给出「采购订单与到货记录 + 时间窗」的中文说法，LLM 才有可抄的业务口径。
    """
    excludedNote = f"；剔除源侧数量异常行 {summary['excludedRows']} 行" if summary["excludedRows"] else ""
    return (
        f"到货率 {summary['orderArrivalRate']}%（全到货 {summary['allArrivedOrders']}"
        f" / 部分 {summary['partialArrivedOrders']} / 未到货 {summary['notArrivedOrders']}，"
        f"共 {summary['totalOrders']} 单）；量级加权到货率 {summary['qtyArrivalRate']}%"
        f"{excludedNote}。"
        f"来源：采购订单与到货记录，统计窗口 {window.dateFrom.isoformat()} ~ "
        f"{window.dateTo.isoformat()}。"
    )


async def deliveryTrackingHandler(session: AsyncSession, args: dict, ctx: AgentToolContext) -> ToolResult:
    """`delivery_tracking_query`：到货率汇总 + 供应商分组 + 订单明细。

    数据源固定 THBI（`datasource_id=1`，由绑定解析闸门保证），物理表列全部
    来自本体绑定；调用方给 `datasourceId` 一律拒绝（不是工具参数）。
    """
    if "datasourceId" in args:
        raise ValidationError(
            "delivery_tracking_query 的数据源固定为 THBI，不接受调用方指定数据源（datasourceId）"
        )
    window = _resolveTrackedWindow(args)
    # 供应商标识可能是编码/企业键/全名/简称 ⇒ 先过解析阶梯再进 SQL。
    supplierKeys, orderKeys, limit = await _resolveFilterArgs(session, args)
    order, receipt = await _resolveTrackedBindings(session)
    ds = await _loadActiveDataSource(session, order.datasourceId)
    adapter = _getAdapter(order.datasourceId, ds)

    summaryRows, supplierRows, orderRows, totalOrders = await _loadDeliveryTrackingRows(
        adapter=adapter,
        order=order,
        receipt=receipt,
        window=window,
        supplierKeys=supplierKeys,
        orderKeys=orderKeys,
        limit=limit,
    )
    directory = await loadMdmDirectory(
        session, entityType="SUPPLIER", codes=_supplierCodesOf([*supplierRows, *orderRows])
    )
    summary = _assembleSummary(summaryRows[0] if summaryRows else {})
    bySupplier = [_assembleSupplier(row) for row in _withSupplierDirectory(supplierRows, directory)]
    byOrder = _assembleOrders(_withSupplierDirectory(orderRows, directory))

    return ToolResult(
        data=_fitResultData(
            _buildResultData(
                datasourceId=order.datasourceId,
                window=window,
                summary=summary,
                bySupplier=bySupplier,
                byOrder=byOrder,
                ordersTruncated=totalOrders > len(byOrder),
            ),
            totalOrders=totalOrders,
        ),
        answer=_buildAnswer(summary, window),
    )
