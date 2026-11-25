"""待到货场景的轻量统计端点（欢迎态「业务现状」卡）。

为什么独立于编排：欢迎态要求**进入即出数**，走 run 要等 worker 且烧 token。
本路由直接调 `pendingDeliveryHandler`（同一段只读 SQL），零 LLM 成本。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import DomainError
from app.services.agent_tool_types import AgentToolContext
from app.services.agent_tools_procurement import (
    _adapterFor,
    _loadActiveDatasource,
    pendingDeliveryHandler,
)
from app.services.pending_delivery_query import (
    _camelPendingRow,
    buildPendingDeliveryCountSql,
    buildPendingDeliveryFilters,
    buildPendingDeliveryLinesSql,
    enrichPendingLineRows,
    resolveFacilityBinding,
    resolvePendingDeliveryBindings,
    validateDateArg,
)
from app.services.procurement_query_utils import SqlDialect as _PendingDialect

router = APIRouter(tags=["procurement-analytics"])


def _today() -> date:
    return date.today()


def _col(row: dict[str, Any], name: str) -> Any:
    """列名大小写不敏感取值（adapter 返回小写键，见 agent_tools_procurement._col）。"""
    if name in row:
        return row[name]
    lowered = name.lower()
    if lowered in row:
        return row[lowered]
    return row.get(name.upper())


def _splitKeys(raw: str | None) -> tuple[str, ...]:
    """逗号分隔的多值参数 → 元组。

    为什么用逗号而不用重复键（``?a=1&a=2``）：axios 默认把数组序列化成
    ``key[]=v``，与 FastAPI 的 ``list[str]`` 参数名对不上 ⇒ 静默收到空列表。
    逗号分隔在两边都稳定。
    """
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _dateSlots(
    dateFrom: str | None, dateTo: str | None
) -> tuple[str | None, str | None]:
    """Query 参数日期校验（非法 ⇒ ValidationError ⇒ 422）。"""
    return (
        validateDateArg(dateFrom, "dateFrom"),
        validateDateArg(dateTo, "dateTo"),
    )


@router.get(
    "/pending-delivery/summary",
    summary="待到货业务现状（轻量统计，不进编排）",
)
async def getPendingDeliverySummary(
    monthsBack: int = Query(default=12, ge=1, le=36),
    supplierKeys: str | None = Query(default=None, description="逗号分隔"),
    materialKeys: str | None = Query(default=None, description="逗号分隔"),
    siteKeys: str | None = Query(default=None, description="逗号分隔"),
    dateFrom: str | None = Query(default=None),
    dateTo: str | None = Query(default=None),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> dict[str, Any]:
    """欠交汇总 + 按供应商 + TOP 行（与工具同口径，直接只读 SQL）。

    数据源固定 THBI（本体绑定唯一确定）；前端历史参数 `datasourceId` 不再接线。
    """
    dateFrom, dateTo = _dateSlots(dateFrom, dateTo)
    supplierList = _splitKeys(supplierKeys)
    materialList = _splitKeys(materialKeys)
    siteList = _splitKeys(siteKeys)
    args: dict[str, Any] = {
        "monthsBack": monthsBack,
        "supplierKeys": list(supplierList),
        "materialKeys": list(materialList),
        "siteKeys": list(siteList),
    }
    if dateFrom:
        args["dateFrom"] = dateFrom
    if dateTo:
        args["dateTo"] = dateTo
    ctx = AgentToolContext(actor=str(getattr(currentUser, "userId", "") or "runtime"))
    try:
        result = await pendingDeliveryHandler(session, args, ctx)
    except DomainError:
        # 认证/参数/冲突类错误保持原语义（401/403/404/409/422），不转 502
        raise
    except Exception as exc:  # noqa: BLE001 — 仅数据源执行失败（方言/缺表/连接）友好化：
        # 未处理异常会绕过 CORS 中间件 ⇒ 浏览器只见 CORS 错误（2026-11-24 实测）
        raise HTTPException(
            status_code=502,
            detail=f"该数据源执行待到货查询失败：{str(exc)[:200]}",
        ) from exc
    data = dict(result.data)
    data["filters"] = {
        "supplierKeys": list(supplierList),
        "materialKeys": list(materialList),
        "siteKeys": list(siteList),
        "dateFrom": dateFrom,
        "dateTo": dateTo,
    }
    return data


@router.get(
    "/pending-delivery/sites",
    summary="基地下拉数据源（FACILITY 字典 ∩ 订单实际出现过的基地）",
)
async def listPendingDeliverySites(
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[dict[str, str]]:
    """基地编码 + 名称（模糊下拉的选项来源，只读）。

    基地字典与订单行站点列都来自本体绑定（`DIM_FACILITY` / 订单行的站点属性），
    不硬编码 schema / 表名。
    """
    order, _ = await resolvePendingDeliveryBindings(session)
    facility = await resolveFacilityBinding(session)
    ds = await _loadActiveDatasource(session, order.datasourceId)
    _ensureActiveAnalytics(ds)
    adapter = _adapterFor(order.datasourceId, ds)
    fcy = facility.column("FCY_0")
    fcynam = facility.column("FCYNAM_0")
    orderSite = order.column("PUR_SITE_CODE")
    tail = _PendingDialect.fromAdapter(adapter).tail(100)
    rows: list[dict[str, Any]]
    try:
        rows = await adapter.execute_read_only(
            f"SELECT F.{fcy} AS FCY_0, F.{fcynam} AS FCYNAM_0 "
            f"FROM {facility.qualifiedTable()} F "
            f"WHERE EXISTS (SELECT 1 FROM {order.qualifiedTable()} O"
            f" WHERE O.{orderSite} = F.{fcy}) "
            f"ORDER BY F.{fcy} {tail}")
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001 — 同 summary：数据源侧失败友好化
        raise HTTPException(
            status_code=502,
            detail=f"该数据源读取基地字典失败：{str(exc)[:200]}",
        ) from exc
    return [
        {"code": str(_col(r, "FCY_0") or ""),
         "name": str(_col(r, "FCYNAM_0") or "")}
        for r in rows
    ]


def _ensureActiveAnalytics(ds: Any) -> None:
    from app.services.datasource_service import ensureDatasourceActive

    ensureDatasourceActive(ds)


#: 明细页单页行数上限（工具侧 50；明细页允许更多，便于翻页浏览）。
_LINES_PAGE_MAX = 200


@router.get(
    "/pending-delivery/lines",
    summary="待到货欠交明细（分页，供明细页使用）",
)
async def listPendingDeliveryLines(
    monthsBack: int = Query(default=12, ge=1, le=36),
    supplierKeys: str | None = Query(default=None, description="逗号分隔"),
    materialKeys: str | None = Query(default=None, description="逗号分隔"),
    siteKeys: str | None = Query(default=None, description="逗号分隔"),
    dateFrom: str | None = Query(default=None),
    dateTo: str | None = Query(default=None),
    minOverdueDays: int = Query(default=0, ge=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=_LINES_PAGE_MAX),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> dict[str, Any]:
    """欠交明细（分页 + 总数）——与工具同口径；明细页专用（工具 limit 上限 50）。"""
    dateFrom, dateTo = _dateSlots(dateFrom, dateTo)
    order, receipt = await resolvePendingDeliveryBindings(session)
    siteList = _splitKeys(siteKeys)
    facility = await resolveFacilityBinding(session) if siteList else None
    filters = await buildPendingDeliveryFilters(
        session,
        orderBinding=order,
        receiptBinding=receipt,
        facilityBinding=facility,
        supplierKeys=_splitKeys(supplierKeys),
        materialKeys=_splitKeys(materialKeys),
        siteKeys=siteList,
        dateFrom=dateFrom,
        dateTo=dateTo,
    )
    try:
        ds = await _loadActiveDatasource(session, order.datasourceId)
        _ensureActiveAnalytics(ds)
        adapter = _adapterFor(order.datasourceId, ds)
        dialect = _PendingDialect.fromAdapter(adapter).name
        countRows = await adapter.execute_read_only(
            buildPendingDeliveryCountSql(
                monthsBack=monthsBack, today=_today(), filters=filters, dialect=dialect
            )
        )
        rows = await adapter.execute_read_only(
            buildPendingDeliveryLinesSql(
                monthsBack=monthsBack, today=_today(), filters=filters,
                minOverdueDays=minOverdueDays or None, limit=limit,
                offset=offset, dialect=dialect,
            )
        )
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001 — 数据源侧失败友好化（未处理异常绕过 CORS）
        raise HTTPException(
            status_code=502,
            detail=f"该数据源执行待到货明细查询失败：{str(exc)[:200]}",
        ) from exc

    total = int(_col(countRows[0], "TOTAL_ROWS") or 0) if countRows else 0
    enriched = await enrichPendingLineRows(session, rows)
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [_camelPendingRow(row) for row in enriched],
    }
