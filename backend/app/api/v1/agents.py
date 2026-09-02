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

from fastapi import APIRouter, Depends, Query, Request, status
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
    AgentOptionsRead,
    AgentToolOption,
)
from app.domain.agent_vocabulary import AGENT_DATA_DOMAINS, AGENT_DATA_LAYERS
from app.services.agent_registry_service import (
    AgentRegistryService,
    _policyToRead,
    agentToRead,
)
from app.services.agent_tools import agent_tool_registry
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.agent_scheduler_service import AgentSchedulerService
from app.domain.schemas import (
    AgentScheduleCreate,
    AgentScheduleRead,
    AgentRunLogRead,
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
    dataDomain: list[str] | None = Query(default=None, alias="dataDomain"),
    dataLayer: str | None = Query(default=None, alias="dataLayer"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
    service: AgentRegistryService = Depends(getAgentRegistryService),
) -> list[AgentDefinitionRead]:
    agents = await service.listAgents(
        session,
        status=status_,
        data_layer=dataLayer,
        data_domains=dataDomain,
        limit=limit,
        offset=offset,
    )
    return [agentToRead(a) for a in agents]


@router.get("/options", response_model=AgentOptionsRead)
async def getAgentOptions(
    _user: CurrentUser = Depends(getCurrentUser),
) -> AgentOptionsRead:
    """Agent 编辑选项（域/层词表 + 工具列表）；前端 useAgentOptions() 缓存。

    必须在 GET /{agent_code} 之前注册——否则 "options" 会被路径参数吞成
    agent_code='options'，触发 getAgent → 404。测试 test_agent_options_api.py 守护。

    tools 来自 ``agent_tool_registry.all()``（按 name 排序）；前端 Agent 表单的
    tool 下拉数据源，避免额外请求。
    """
    return AgentOptionsRead(
        domains=list(AGENT_DATA_DOMAINS),
        layers=list(AGENT_DATA_LAYERS),
        tools=[
            AgentToolOption(
                name=t.name,
                description=t.description,
                data_object=t.data_object,
                data_layers=list(t.data_layers),
            )
            for t in agent_tool_registry.all()
        ],
    )


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


# ---------------------------------------------------------------------------
# Agent 定时调度（嵌套在 agent 下，Phase 7 G5）
# ---------------------------------------------------------------------------


def getAgentSchedulerService() -> AgentSchedulerService:
    return AgentSchedulerService()


@router.post(
    "/{agent_code}/schedules",
    response_model=AgentScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Agent 调度（cron 表达式 + 自然语言输入）",
)
@limiter.limit(rateLimitValue)
async def createSchedule(
    request: Request,
    agent_code: str,
    payload: AgentScheduleCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentSchedulerService = Depends(getAgentSchedulerService),
) -> AgentScheduleRead:
    return await service.createSchedule(session, agent_code, payload, user)


@router.get(
    "/{agent_code}/schedules",
    response_model=list[AgentScheduleRead],
    summary="列出指定 Agent 的全部调度",
)
async def listSchedules(
    agent_code: str,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentSchedulerService = Depends(getAgentSchedulerService),
) -> list[AgentScheduleRead]:
    return await service.listSchedules(session, agent_code, user)


@router.patch(
    "/{agent_code}/schedules/{schedule_id}/toggle",
    response_model=AgentScheduleRead,
    summary="启停调度（is_active 翻转）",
)
@limiter.limit(rateLimitValue)
async def toggleSchedule(
    request: Request,
    agent_code: str,
    schedule_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentSchedulerService = Depends(getAgentSchedulerService),
) -> AgentScheduleRead:
    return await service.toggleSchedule(session, agent_code, schedule_id, user)


@router.get(
    "/{agent_code}/schedules/logs",
    response_model=list[AgentRunLogRead],
    summary="调度运行历史（前端 schedule 面板数据源）",
)
async def listRunLogs(
    agent_code: str,
    user: CurrentUser = Depends(getCurrentUser),
    schedule_id: int | None = Query(default=None),
    session: AsyncSession = Depends(getDb),
    service: AgentSchedulerService = Depends(getAgentSchedulerService),
) -> list[AgentRunLogRead]:
    return await service.listRunLogs(session, agent_code, user, schedule_id=schedule_id)


@router.delete(
    "/{agent_code}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除调度（运行日志保留）",
)
@limiter.limit(rateLimitValue)
async def deleteSchedule(
    request: Request,
    agent_code: str,
    schedule_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentSchedulerService = Depends(getAgentSchedulerService),
) -> None:
    await service.deleteSchedule(session, agent_code, schedule_id, user)