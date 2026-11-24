"""待到货场景的轻量统计端点（欢迎态「业务现状」卡）。

为什么独立于编排：欢迎态要求**进入即出数**，走 run 要等 worker 且烧 token。
本路由直接调 `pendingDeliveryHandler`（同一段只读 SQL），零 LLM 成本。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import DomainError
from app.services.agent_tool_types import AgentToolContext
from app.services.agent_tools_procurement import (
    PendingDeliveryFilters,
    _adapterFor,
    _resolveBusinessDatasource,
    pendingDeliveryHandler,
)

router = APIRouter(tags=["procurement-analytics"])


def _filtersFromQuery(
    supplierKeys: list[str],
    materialKeys: list[str],
    siteKeys: list[str],
    dateFrom: str | None,
    dateTo: str | None,
) -> PendingDeliveryFilters:
    """Query 参数 → filters（非法日期在这里抛 ValidationError ⇒ 422）。"""
    return PendingDeliveryFilters(
        supplierKeys=tuple(supplierKeys),
        materialKeys=tuple(materialKeys),
        siteKeys=tuple(siteKeys),
        dateFrom=dateFrom,
        dateTo=dateTo,
    )


@router.get(
    "/pending-delivery/summary",
    summary="待到货业务现状（轻量统计，不进编排）",
)
async def getPendingDeliverySummary(
    datasourceId: int | None = Query(default=None, ge=1),
    monthsBack: int = Query(default=12, ge=1, le=36),
    supplierKeys: list[str] = Query(default=[]),
    materialKeys: list[str] = Query(default=[]),
    siteKeys: list[str] = Query(default=[]),
    dateFrom: str | None = Query(default=None),
    dateTo: str | None = Query(default=None),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> dict[str, Any]:
    """欠交汇总 + 按供应商 + TOP 行（与工具同口径，直接只读 SQL）。"""
    filters = _filtersFromQuery(
        supplierKeys, materialKeys, siteKeys, dateFrom, dateTo
    )
    args: dict[str, Any] = {
        "monthsBack": monthsBack,
        "supplierKeys": list(filters.supplierKeys),
        "materialKeys": list(filters.materialKeys),
        "siteKeys": list(filters.siteKeys),
    }
    if filters.dateFrom:
        args["dateFrom"] = filters.dateFrom
    if filters.dateTo:
        args["dateTo"] = filters.dateTo
    if datasourceId is not None:
        args["datasourceId"] = datasourceId
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
        "supplierKeys": list(filters.supplierKeys),
        "materialKeys": list(filters.materialKeys),
        "siteKeys": list(filters.siteKeys),
        "dateFrom": filters.dateFrom,
        "dateTo": filters.dateTo,
    }
    return data


@router.get(
    "/pending-delivery/sites",
    summary="基地下拉数据源（FACILITY 字典 ∩ 订单实际出现过的基地）",
)
async def listPendingDeliverySites(
    datasourceId: int | None = Query(default=None, ge=1),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[dict[str, str]]:
    """基地编码 + 名称（模糊下拉的选项来源，只读）。"""
    ds = await _resolveBusinessDatasource(session, currentUser)
    _ensureActiveAnalytics(ds)
    adapter = _adapterFor(ds.id, ds)
    rows: list[dict[str, Any]]
    try:
        rows = await adapter.execute_read_only(
            "SELECT F.FCY_0, F.FCYNAM_0 FROM ZJTH.FACILITY F "
            "WHERE EXISTS (SELECT 1 FROM ZJTH.PORDERQ Q WHERE Q.POHFCY_0 = F.FCY_0) "
            "ORDER BY F.FCY_0 FETCH FIRST 100 ROWS ONLY")
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001 — 同 summary：数据源侧失败友好化
        raise HTTPException(
            status_code=502,
            detail=f"该数据源读取基地字典失败：{str(exc)[:200]}",
        ) from exc
    return [
        {"code": str(r.get("fcy_0") or r.get("FCY_0") or ""),
         "name": str(r.get("fcynam_0") or r.get("FCYNAM_0") or "")}
        for r in rows
    ]


def _ensureActiveAnalytics(ds: Any) -> None:
    from app.services.datasource_service import ensureDatasourceActive

    ensureDatasourceActive(ds)
