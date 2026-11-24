"""待到货场景的轻量统计端点（欢迎态「业务现状」卡）。

为什么独立于编排：欢迎态要求**进入即出数**，走 run 要等 worker 且烧 token。
本路由直接调 `pendingDeliveryHandler`（同一段只读 SQL），零 LLM 成本。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.services.agent_tool_types import AgentToolContext
from app.services.agent_tools_procurement import pendingDeliveryHandler

router = APIRouter(tags=["procurement-analytics"])


@router.get(
    "/pending-delivery/summary",
    summary="待到货业务现状（轻量统计，不进编排）",
)
async def getPendingDeliverySummary(
    datasourceId: int | None = Query(default=None, ge=1),
    monthsBack: int = Query(default=12, ge=1, le=36),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> dict[str, Any]:
    """欠交汇总 + 按供应商 + TOP 行（与工具同口径，直接只读 SQL）。"""
    args: dict[str, Any] = {"monthsBack": monthsBack}
    if datasourceId is not None:
        args["datasourceId"] = datasourceId
    ctx = AgentToolContext(actor=str(getattr(currentUser, "userId", "") or "runtime"))
    result = await pendingDeliveryHandler(session, args, ctx)
    return result.data
