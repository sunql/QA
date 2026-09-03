"""AgentToolConfig 管理 API（feat-agent-tool-config-db, T12）。

挂在 /api/v1/agent-tools：
  GET    /api/v1/agent-tools                       列表（enabledOnly 过滤）
  GET    /api/v1/agent-tools/{name}                详情
  POST   /api/v1/agent-tools                       创建（admin only）
  PUT    /api/v1/agent-tools/{name}                更新（admin only, optimistic lock）
  DELETE /api/v1/agent-tools/{name}                删除（admin only, 409 if referenced）
  POST   /api/v1/agent-tools/{name}/toggle         翻转 enabled（admin only）

ACL：读 - 所有登录用户；写 - 仅 admin。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.schemas import (
    AgentToolConfigCreate,
    AgentToolConfigRead,
    AgentToolConfigUpdate,
)
from app.services.agent_tool_config_service import AgentToolConfigService

router = APIRouter(prefix="/api/v1/agent-tools", tags=["agent-tools"])


def _svc() -> AgentToolConfigService:
    return AgentToolConfigService()


@router.get("", response_model=list[AgentToolConfigRead])
async def listAgentTools(
    enabledOnly: bool = False,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[AgentToolConfigRead]:
    """列出所有 AgentToolConfig；enabledOnly=true 过滤禁用项。"""
    rows = await _svc().listTools(session, enabledOnly=enabledOnly)
    return [AgentToolConfigRead.model_validate(r) for r in rows]


@router.get("/{name}", response_model=AgentToolConfigRead)
async def getAgentTool(
    name: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> AgentToolConfigRead:
    """按名查询 AgentToolConfig；不存在 → 404。"""
    row = await _svc().getTool(session, name)
    return AgentToolConfigRead.model_validate(row)


@router.post(
    "", response_model=AgentToolConfigRead, status_code=status.HTTP_201_CREATED
)
async def createAgentTool(
    payload: AgentToolConfigCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> AgentToolConfigRead:
    """创建 AgentToolConfig（admin only）；name 冲突 → 409。"""
    row = await _svc().createTool(session, payload, _admin)
    await session.commit()
    return AgentToolConfigRead.model_validate(row)


@router.put("/{name}", response_model=AgentToolConfigRead)
async def updateAgentTool(
    name: str,
    payload: AgentToolConfigUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> AgentToolConfigRead:
    """更新 AgentToolConfig（admin only, optimistic lock）；version 不匹配 → 409。"""
    row = await _svc().updateTool(session, name, payload, _admin)
    await session.commit()
    return AgentToolConfigRead.model_validate(row)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteAgentTool(
    name: str,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    """删除 AgentToolConfig（admin only）；被 AgentDefinition.tool_name 引用 → 409。"""
    await _svc().deleteTool(session, name, _admin)
    await session.commit()


@router.post("/{name}/toggle", response_model=AgentToolConfigRead)
async def toggleAgentTool(
    name: str,
    enabled: bool = Body(..., embed=True),
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> AgentToolConfigRead:
    """翻转 enabled（admin only）。"""
    row = await _svc().toggleEnabled(session, name, enabled, _admin)
    await session.commit()
    return AgentToolConfigRead.model_validate(row)