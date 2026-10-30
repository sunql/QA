"""Agent 编排运行的 run 生命周期 API（Task 7）。

挂在 /api/v1/agent-runs（feature flag 控制是否注册，见 main.py）：
  POST /agent-runs                    创建运行（202，QUEUED）
  GET  /agent-runs                    列出当前用户自己的 run
  GET  /agent-runs/{runId}            运行详情
  GET  /agent-runs/{runId}/steps      运行轨迹（step_no 升序）
  POST /agent-runs/{runId}/resume     恢复暂停的运行（202，QUEUED）
  POST /agent-runs/{runId}/cancel     取消运行（200，返回当前摘要）

鉴权：所有路由依赖 ``getCurrentUser``（无匿名访问）；create/resume/cancel 额外挂
``@limiter.limit(rateLimitValue)`` 防 DoS 放大（与既有 run 端点一致）。

所有权：run 归创建者所有，读取/恢复/取消时非创建者且非 admin 一律 404
（统一文案，不泄露 run 是否存在）。

``afterStepNo`` 为**闭区间下界**（step_no >= afterStepNo），默认 0 表示从头返回。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.agent_orchestration_schemas import (
    AgentRunCreate,
    AgentRunCreated,
    AgentRunRead,
    AgentRunResume,
    AgentRunStepRead,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.agent_orchestration_run_service import (
    DEFAULT_RUN_LIMIT,
    DEFAULT_STEP_LIMIT,
    AgentOrchestrationRunService,
)
from app.services.agent_orchestration_types import RunStatus

router = APIRouter()


def getAgentOrchestrationRunService() -> AgentOrchestrationRunService:
    return AgentOrchestrationRunService()


@router.post(
    "",
    response_model=AgentRunCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建编排运行（202，QUEUED）",
)
@limiter.limit(rateLimitValue)
async def createRun(
    request: Request,
    payload: AgentRunCreate,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationRunService = Depends(
        getAgentOrchestrationRunService
    ),
) -> AgentRunCreated:
    """创建一次编排运行；任一门槛不满足都不落库（409/422）。"""
    return await service.createRun(session, payload, currentUser)


@router.get(
    "",
    response_model=list[AgentRunRead],
    summary="列出当前用户自己的编排运行",
)
async def listRuns(
    agentCode: str | None = Query(default=None, description="按 Agent code 过滤"),
    runStatus: RunStatus | None = Query(
        default=None,
        alias="status",
        description="按运行状态过滤",
    ),
    limit: int = Query(default=DEFAULT_RUN_LIMIT, ge=0, description="条数（上限 200）"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationRunService = Depends(
        getAgentOrchestrationRunService
    ),
) -> list[AgentRunRead]:
    """只返回调用者自己的 run；limit 超过 200 时按 200 截断。"""
    return await service.listRuns(
        session,
        _currentUser,
        agentCode=agentCode,
        status=runStatus,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{runId}",
    response_model=AgentRunRead,
    summary="查询编排运行详情",
)
async def getRun(
    runId: str,
    _currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationRunService = Depends(
        getAgentOrchestrationRunService
    ),
) -> AgentRunRead:
    """非创建者且非 admin 统一 404。"""
    return await service.getRun(session, runId, _currentUser)


@router.get(
    "/{runId}/steps",
    response_model=list[AgentRunStepRead],
    summary="查询编排运行轨迹（step_no 升序）",
)
async def listSteps(
    runId: str,
    afterStepNo: int = Query(
        default=0,
        ge=0,
        description="起始 step_no（闭区间下界，含该步）",
    ),
    limit: int = Query(
        default=DEFAULT_STEP_LIMIT, ge=0, description="条数（上限 200）"
    ),
    _currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationRunService = Depends(
        getAgentOrchestrationRunService
    ),
) -> list[AgentRunStepRead]:
    """``afterStepNo`` 为闭区间下界：返回 step_no >= afterStepNo 的步骤。"""
    return await service.listSteps(
        session,
        runId,
        _currentUser,
        afterStepNo=afterStepNo,
        limit=limit,
    )


@router.post(
    "/{runId}/resume",
    response_model=AgentRunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="恢复暂停的编排运行（202，QUEUED）",
)
@limiter.limit(rateLimitValue)
async def resumeRun(
    request: Request,
    runId: str,
    payload: AgentRunResume,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationRunService = Depends(
        getAgentOrchestrationRunService
    ),
) -> AgentRunRead:
    """仅 PAUSED 可恢复；按 pause_code 施加输入/预算规则，PERMISSION_DENIED 复查当前 ACL。"""
    return await service.resumeRun(session, runId, payload, currentUser)


@router.post(
    "/{runId}/cancel",
    response_model=AgentRunRead,
    summary="取消编排运行（返回当前摘要）",
)
@limiter.limit(rateLimitValue)
async def cancelRun(
    request: Request,
    runId: str,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: AgentOrchestrationRunService = Depends(
        getAgentOrchestrationRunService
    ),
) -> AgentRunRead:
    """QUEUED/PAUSED 立即取消；RUNNING 只标记取消请求（不伪装成已终止）。"""
    return await service.cancelRun(session, runId, currentUser)
