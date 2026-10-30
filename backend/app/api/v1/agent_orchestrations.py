"""Agent orchestration 配置 API（Task 3）。

挂在 /api/v1/agent-orchestrations（feature flag 控制是否注册，见 main.py）：
  POST /agent-orchestrations                        创建 profile（201，恒 enabled=false）
  GET  /agent-orchestrations                        列表（按 owner 过滤）
  GET  /agent-orchestrations/tool-options           只读工具选项
  GET  /agent-orchestrations/{agentCode}            详情
  PUT  /agent-orchestrations/{agentCode}            全量更新（乐观锁 revision）
  POST /agent-orchestrations/{agentCode}/toggle     显式启停（乐观锁 revision）

鉴权：所有路由依赖 ``getCurrentUser``；写操作在 service 层再过
``AclService.assertCanModify``（admin 或 owner 部门），无权限 → 403。

路由顺序：``/tool-options`` 必须声明在 ``/{agentCode}`` 之前，否则静态段会被
路径参数吞成 Agent code。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.agent_orchestration_schemas import (
    AgentOrchestrationProfileCreate,
    AgentOrchestrationProfileRead,
    AgentOrchestrationProfileUpdate,
    AgentOrchestrationToggle,
    AgentOrchestrationToolOptionRead,
)
from app.services.agent_orchestration_config_service import (
    AgentOrchestrationConfigService,
)

router = APIRouter()


def getAgentOrchestrationConfigService() -> AgentOrchestrationConfigService:
    return AgentOrchestrationConfigService()


@router.post(
    "",
    response_model=AgentOrchestrationProfileRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Agent 编排配置（默认禁用）",
)
async def createProfile(
    payload: AgentOrchestrationProfileCreate,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationConfigService = Depends(
        getAgentOrchestrationConfigService
    ),
) -> AgentOrchestrationProfileRead:
    return await service.create(session, payload, currentUser)


@router.get(
    "",
    response_model=list[AgentOrchestrationProfileRead],
    summary="列出当前用户可见的编排配置",
)
async def listProfiles(
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationConfigService = Depends(
        getAgentOrchestrationConfigService
    ),
) -> list[AgentOrchestrationProfileRead]:
    return await service.list(session, currentUser)


@router.get(
    "/tool-options",
    response_model=list[AgentOrchestrationToolOptionRead],
    summary="可用于编排的只读工具选项（不含 handlerRef）",
)
async def listToolOptions(
    _currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationConfigService = Depends(
        getAgentOrchestrationConfigService
    ),
) -> list[AgentOrchestrationToolOptionRead]:
    return await service.listToolOptions(session)


@router.get(
    "/{agentCode}",
    response_model=AgentOrchestrationProfileRead,
    summary="查询指定 Agent 的编排配置",
)
async def getProfile(
    agentCode: str,
    _currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationConfigService = Depends(
        getAgentOrchestrationConfigService
    ),
) -> AgentOrchestrationProfileRead:
    return await service.get(session, agentCode)


@router.put(
    "/{agentCode}",
    response_model=AgentOrchestrationProfileRead,
    summary="全量更新编排配置（revision 乐观锁）",
)
async def updateProfile(
    agentCode: str,
    payload: AgentOrchestrationProfileUpdate,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationConfigService = Depends(
        getAgentOrchestrationConfigService
    ),
) -> AgentOrchestrationProfileRead:
    return await service.update(session, agentCode, payload, currentUser)


@router.post(
    "/{agentCode}/toggle",
    response_model=AgentOrchestrationProfileRead,
    summary="显式启用 / 停用编排配置（revision 乐观锁）",
)
async def toggleProfile(
    agentCode: str,
    payload: AgentOrchestrationToggle,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationConfigService = Depends(
        getAgentOrchestrationConfigService
    ),
) -> AgentOrchestrationProfileRead:
    return await service.toggle(session, agentCode, payload, currentUser)
