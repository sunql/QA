"""Agent Registry REST API（Phase 6.1）。

挂在 /api/v1/agents：
  GET    /api/v1/agents                       列表（按 status / dataDomain 过滤）
  GET    /api/v1/agents/{agent_code}          详情
  POST   /api/v1/agents                       创建（201）
  PUT    /api/v1/agents/{agent_code}          更新（admin / owner）
  DELETE /api/v1/agents/{agent_code}          软删除（status → deprecated，admin / owner）

  GET    /api/v1/agents/{agent_code}/policies        列出访问策略
  POST   /api/v1/agents/{agent_code}/policies        追加策略（admin / owner）
  PUT    /api/v1/agents/{agent_code}/policies/{pid}  更新策略
  DELETE /api/v1/agents/{agent_code}/policies/{pid}  删除策略

ACL：读 - 所有登录用户；写 - admin 或 owner 部门成员（Phase 4.5 AclService.assertCanModify）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.enums import AgentStatus
from app.domain.schemas import (
    AgentAccessPolicyCreate,
    AgentAccessPolicyRead,
    AgentAccessPolicyUpdate,
    AgentDefinitionCreate,
    AgentDefinitionRead,
    AgentDefinitionUpdate,
)
from app.services.agent_registry_service import (
    AgentRegistryService,
    _policyToRead,
    agentToRead,
)

router = APIRouter()


def getAgentRegistryService() -> AgentRegistryService:
    return AgentRegistryService()


# ---------------------------------------------------------------------------
# Agent 定义 CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[AgentDefinitionRead])
async def listAgents(
    _user: CurrentUser = Depends(getCurrentUser),
    status_: AgentStatus | None = Query(default=None, alias="status"),
    dataDomain: str | None = Query(default=None, alias="dataDomain"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> list[AgentDefinitionRead]:
    agents = await service.listAgents(
        session,
        status=status_,
        data_domain=dataDomain,
        limit=limit,
        offset=offset,
    )
    return [agentToRead(a) for a in agents]


@router.get("/{agent_code}", response_model=AgentDefinitionRead)
async def getAgent(
    agent_code: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> AgentDefinitionRead:
    entity = await service.getAgent(session, agent_code)
    return agentToRead(entity)


@router.post("", response_model=AgentDefinitionRead, status_code=status.HTTP_201_CREATED)
async def createAgent(
    payload: AgentDefinitionCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> AgentDefinitionRead:
    entity = await service.createAgent(session, payload, user)
    return agentToRead(entity)


@router.put("/{agent_code}", response_model=AgentDefinitionRead)
async def updateAgent(
    agent_code: str,
    payload: AgentDefinitionUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> AgentDefinitionRead:
    entity = await service.updateAgent(session, agent_code, payload, user)
    return agentToRead(entity)


@router.delete("/{agent_code}", response_model=AgentDefinitionRead)
async def deprecateAgent(
    agent_code: str,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> AgentDefinitionRead:
    entity = await service.deprecateAgent(session, agent_code, user)
    return agentToRead(entity)


# ---------------------------------------------------------------------------
# 访问策略 CRUD（嵌套在 agent 下）
# ---------------------------------------------------------------------------


@router.get("/{agent_code}/policies", response_model=list[AgentAccessPolicyRead])
async def listAgentPolicies(
    agent_code: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> list[AgentAccessPolicyRead]:
    policies = await service.listPolicies(session, agent_code)
    return [_policyToRead(p) for p in policies]


@router.post(
    "/{agent_code}/policies",
    response_model=AgentAccessPolicyRead,
    status_code=status.HTTP_201_CREATED,
)
async def addAgentPolicy(
    agent_code: str,
    payload: AgentAccessPolicyCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> AgentAccessPolicyRead:
    policy = await service.addPolicy(session, agent_code, payload, user)
    return _policyToRead(policy)


@router.put(
    "/{agent_code}/policies/{policy_id}",
    response_model=AgentAccessPolicyRead,
)
async def updateAgentPolicy(
    agent_code: str,
    policy_id: int,
    payload: AgentAccessPolicyUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> AgentAccessPolicyRead:
    policy = await service.updatePolicy(session, agent_code, policy_id, payload, user)
    return _policyToRead(policy)


@router.delete(
    "/{agent_code}/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deleteAgentPolicy(
    agent_code: str,
    policy_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> None:
    await service.deletePolicy(session, agent_code, policy_id, user)