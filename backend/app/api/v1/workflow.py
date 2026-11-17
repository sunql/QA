"""P5 工作流 / 审批 API。

挂在 ``/api/v1/workflow``（feature flag 控制是否注册，见 ``main.py``）：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | ``/instances`` | 提交工作流实例（**幂等**：同 idempotencyKey 只落一行） |
| GET | ``/instances/{instanceId}`` | 实例详情（实例 + 步骤 + 审批任务） |
| POST | ``/instances/{instanceId}/approval-request`` | 发起一轮审批（按矩阵生成审批任务） |
| GET | ``/instances/{instanceId}/approvals`` | 列出该实例的审批任务 |
| POST | ``/approvals/{taskId}/decision`` | 审批决策（APPROVE / REJECT） |
| POST | ``/instances/{instanceId}/cancel`` | 取消实例（提交人或 admin；待执行命令一并取消） |

**鉴权**：全部路由依赖 ``getCurrentUser``（无匿名访问）。写入口用
``enforceRouteLimit`` 显式限流（``@limiter.limit`` 在本仓是死代码，见
``app/infrastructure/rate_limit.py`` 的说明）。

**审批决策额外要求已校验身份**（``CurrentUser.isRealIdentity``：JWT 验签过 ⇒
``jti`` 非空）：stub 头可被任意客户端伪造（``X-User-Id: lead`` 即冒充 lead）
⇒ 审批人必须是**验过签的人**（四眼原则 + 审计可信）。

**七守卫**在服务层（``workflow_approval_service``）：越权 403 / 重复审批 409 /
超时 409 / 对象级不可见 404 ⇒ 拒绝即零副作用。

⚠️ 开关关闭时整组路由**不注册**（404）——「没开 = 没有这个面」。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import PermissionDeniedError
from app.domain.workflow_schemas import (
    WorkflowApprovalDecision,
    WorkflowApprovalRead,
    WorkflowApprovalRequest,
    WorkflowInstanceCancel,
    WorkflowInstanceCreate,
    WorkflowInstanceDetail,
    WorkflowInstanceRead,
    WorkflowStepRead,
)
from app.infrastructure.rate_limit import enforceRouteLimit, rateLimitValue
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_service import WorkflowService

router = APIRouter()

# 限流桶作用域（**常量**：URL 路径会随挂载前缀变化 ⇒ 不能拿路径当键）。
SCOPE_SUBMIT_INSTANCE = "workflow:submit-instance"
SCOPE_REQUEST_APPROVAL = "workflow:request-approval"
SCOPE_APPROVAL_DECISION = "workflow:approval-decision"
SCOPE_CANCEL_INSTANCE = "workflow:cancel-instance"

# 审批决策要求真实身份（stub 身份可伪造 ⇒ 不能用于审批）。
DETAIL_APPROVAL_REAL_AUTH_REQUIRED = "APPROVAL_REAL_AUTH_REQUIRED"
MSG_APPROVAL_REAL_AUTH_REQUIRED = (
    "审批决策要求真实登录身份（stub 身份可被任意客户端伪造，不可用于审批）"
)


def getWorkflowService() -> WorkflowService:
    return WorkflowService()


def getWorkflowApprovalService() -> WorkflowApprovalService:
    return WorkflowApprovalService()


@router.post(
    "/instances",
    response_model=WorkflowInstanceRead,
    status_code=status.HTTP_201_CREATED,
    summary="提交工作流实例（幂等）",
)
async def submitInstance(
    request: Request,
    response: Response,
    payload: WorkflowInstanceCreate,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: WorkflowService = Depends(getWorkflowService),
) -> WorkflowInstanceRead:
    """提交实例；同 ``idempotencyKey`` 重复提交返回既有实例（不重复建步骤）。

    201 = 本次新建；200 = 幂等重放（返回既有实例）。
    """

    enforceRouteLimit(request, rateLimitValue(), SCOPE_SUBMIT_INSTANCE)

    instance, created = await service.submitInstance(
        session,
        domainCode=payload.domain_code,
        capabilityCode=payload.capability_code,
        version=payload.version,
        idempotencyKey=payload.idempotency_key,
        requestJson=payload.request,
        actor=currentUser.userId,
        actorDepartments=currentUser.departments,
    )
    await session.commit()
    if not created:
        response.status_code = status.HTTP_200_OK
    return WorkflowInstanceRead.model_validate(instance)


@router.get(
    "/instances/{instanceId}",
    response_model=WorkflowInstanceDetail,
    summary="工作流实例详情",
)
async def getInstance(
    instanceId: int,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: WorkflowService = Depends(getWorkflowService),
    approvalService: WorkflowApprovalService = Depends(getWorkflowApprovalService),
) -> WorkflowInstanceDetail:
    instance = await approvalService.assertInstanceVisible(
        session,
        instanceId,
        actor=currentUser.userId,
        roles=currentUser.roles,
        isRealIdentity=currentUser.isRealIdentity,
    )
    steps = await service.listSteps(session, instanceId)
    approvals = await approvalService.listApprovals(session, instanceId=instanceId)
    return WorkflowInstanceDetail(
        instance=WorkflowInstanceRead.model_validate(instance),
        steps=[WorkflowStepRead.model_validate(step) for step in steps],
        approvals=[WorkflowApprovalRead.model_validate(task) for task in approvals],
    )


@router.post(
    "/instances/{instanceId}/approval-request",
    response_model=list[WorkflowApprovalRead],
    summary="发起一轮审批（按矩阵生成审批任务）",
)
async def requestApproval(
    request: Request,
    instanceId: int,
    payload: WorkflowApprovalRequest,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    approvalService: WorkflowApprovalService = Depends(getWorkflowApprovalService),
) -> list[WorkflowApprovalRead]:
    """按审批矩阵生成审批任务。

    **风险级别由服务端（定义体）绑定** —— 请求体不含 riskLevel（多传会被
    ``extra="forbid"`` 拒成 422）。矩阵未覆盖该组合 ⇒ **失败关闭**（409，不落任何行）。
    """

    enforceRouteLimit(request, rateLimitValue(), SCOPE_REQUEST_APPROVAL)

    tasks = await approvalService.requestApproval(
        session,
        instanceId=instanceId,
        actor=currentUser.userId,
        roles=currentUser.roles,
        actorDepartments=currentUser.departments,
        isRealIdentity=currentUser.isRealIdentity,
    )
    await session.commit()
    return [WorkflowApprovalRead.model_validate(task) for task in tasks]


@router.get(
    "/instances/{instanceId}/approvals",
    response_model=list[WorkflowApprovalRead],
    summary="列出实例的审批任务",
)
async def listApprovals(
    instanceId: int,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    approvalService: WorkflowApprovalService = Depends(getWorkflowApprovalService),
) -> list[WorkflowApprovalRead]:
    tasks = await approvalService.listVisibleApprovals(
        session,
        instanceId,
        actor=currentUser.userId,
        roles=currentUser.roles,
        isRealIdentity=currentUser.isRealIdentity,
    )
    return [WorkflowApprovalRead.model_validate(task) for task in tasks]


@router.post(
    "/approvals/{taskId}/decision",
    response_model=WorkflowApprovalRead,
    summary="审批决策（APPROVE / REJECT）",
)
async def decideApproval(
    request: Request,
    taskId: int,
    payload: WorkflowApprovalDecision,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: WorkflowService = Depends(getWorkflowService),
) -> WorkflowApprovalRead:
    """审批决策。

    七守卫在服务层：对象级不可见 404 / 越权 404（与「任务不存在」逐字同形 ⇒
    无存在性预言机）/ 自审批 403（actor 本就能看见这条任务 ⇒ 明说四眼规矩）/
    重复审批 409 / 超时 409。

    **要求已校验身份**（``isRealIdentity``：JWT 验签过 ⇒ ``jti`` 非空）：
    stub 头可被任意客户端伪造（``X-User-Id: lead`` 即冒充 lead）⇒ 四眼原则
    与「谁批的」审计都必须是**验过签的人**。

    ⚠️ **两段事务**（在 ``WorkflowService.decideApprovalAndAdvance`` 里）：
    决策先落库（**决策是事实**）⇒ 推进失败**不会**把已发生的审批抹掉；
    推进失败 ⇒ 实例失败关闭（``FAILED``）⇒ 不会「看起来还在跑」。
    """

    enforceRouteLimit(request, rateLimitValue(), SCOPE_APPROVAL_DECISION)

    if not currentUser.isRealIdentity:
        raise PermissionDeniedError(
            MSG_APPROVAL_REAL_AUTH_REQUIRED,
            detail=DETAIL_APPROVAL_REAL_AUTH_REQUIRED,
        )

    task = await service.decideApprovalAndAdvance(
        session,
        taskId=taskId,
        decision=payload.decision,
        actor=currentUser.userId,
        roles=currentUser.roles,
        note=payload.note,
        actorDepartments=currentUser.departments,
    )
    return WorkflowApprovalRead.model_validate(task)


@router.post(
    "/instances/{instanceId}/cancel",
    response_model=WorkflowInstanceRead,
    summary="取消工作流实例（提交人或 admin）",
)
async def cancelInstance(
    request: Request,
    instanceId: int,
    payload: WorkflowInstanceCancel,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: WorkflowService = Depends(getWorkflowService),
) -> WorkflowInstanceRead:
    """取消工作流实例。

    取消 = **真的不再写外部系统**：待审批任务 → CANCELLED；待执行命令 →
    CANCELLED（**不再被 Worker 领取**）；未开始的步骤 → SKIPPED。

    - **身份**：必须**已校验**（``isRealIdentity``：JWT 验签过）⇒ 否则 403
      （stub 头可冒充任意 DB 账号 ⇒ 治理写面必须验签）；
    - 权限：只有**提交人**（自作用域）或**真实** admin ⇒ 其它人 **404**
      （与「实例不存在」同码 ⇒ 不泄露存在性）；
    - 实例已是终态 ⇒ 409；重复取消 ⇒ **幂等**（200，返回既有实例）。
    """

    enforceRouteLimit(request, rateLimitValue(), SCOPE_CANCEL_INSTANCE)

    instance = await service.cancelInstance(
        session,
        instanceId=instanceId,
        actor=currentUser.userId,
        roles=currentUser.roles,
        dbUserId=currentUser.dbUserId,
        isRealIdentity=currentUser.isRealIdentity,
        reason=payload.reason,
        actorDepartments=currentUser.departments,
    )
    await session.commit()
    return WorkflowInstanceRead.model_validate(instance)
