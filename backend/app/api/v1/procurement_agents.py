"""M3-3 采购 Agent 场景目录：`GET /api/v1/procurement/agents/catalog`。

设计稿 §8.4：「返回采购 Agent 场景目录、可用工具和能力说明，**不暴露
handlerRef**」。目录口径：`agent_code` 以 `PROCUREMENT_` 开头的场景 Agent
（`PROCUREMENT_COPILOT` 占位、`SUPPLIER_360_AGENT` 等全局 Agent 不进目录）。
视图变换在 `services/agent_catalog.buildProcurementCatalog`（纯函数）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.knowledge_schemas import ProcurementAgentCatalogEntryRead
from app.domain.models import AgentDefinition, AgentToolConfig
from app.services.agent_catalog import buildProcurementCatalog

router = APIRouter(tags=["procurement-agents"])

_CATALOG_PREFIX = "PROCUREMENT_"


@router.get(
    "/agents/catalog",
    response_model=list[ProcurementAgentCatalogEntryRead],
)
async def getProcurementAgentCatalog(
    response: Response,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[ProcurementAgentCatalogEntryRead]:
    """采购 Agent 场景目录：场景 + 可用工具 + 能力说明 + 可运行性。"""
    response.headers["Cache-Control"] = "no-store"
    agentRows = (
        await session.execute(
            select(AgentDefinition)
            .where(AgentDefinition.agent_code.startswith(_CATALOG_PREFIX))
            .order_by(AgentDefinition.agent_code)
        )
    ).scalars().all()
    toolConfigs = (
        await session.execute(select(AgentToolConfig))
    ).scalars().all()
    toolConfigByName = {cfg.name: cfg for cfg in toolConfigs}
    return buildProcurementCatalog(list(agentRows), toolConfigByName)
