"""P5 工作流 / 审批 API。

挂在 ``/api/v1/workflow``（feature flag 控制是否注册，见 ``main.py``）：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | ``/instances`` | 提交工作流实例（**幂等**：同 idempotencyKey 只落一行） |
| GET | ``/instances`` | 实例列表（自作用域 / 审批持有人 / 真实 admin） |
| GET | ``/instances/{instanceId}`` | 实例详情（实例 + 步骤 + 审批任务） |
| POST | ``/instances/{instanceId}/approval-request`` | 发起一轮审批（按矩阵生成审批任务） |
| GET | ``/instances/{instanceId}/approvals`` | 列出该实例的审批任务 |
| GET | ``/approvals`` | **我的待办**（审批持有人匹配；要求已校验身份） |
| POST | ``/approvals/{taskId}/decision`` | 审批决策（APPROVE / REJECT） |
| POST | ``/instances/{instanceId}/cancel`` | 取消实例（提交人或 admin；待执行命令一并取消） |
| GET | ``/definitions`` | 已发布定义列表（提交表单候选集；**不含定义体**） |

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

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import PermissionDeniedError
from app.domain.workflow_models import (
    ApprovalTask,
    WorkflowDefinition,
    WorkflowInstance,
)
from app.domain.workflow_schemas import (
    WorkflowApprovalDecision,
    WorkflowApprovalListItem,
    WorkflowApprovalPage,
    WorkflowApprovalRead,
    WorkflowApprovalRequest,
    WorkflowDefinitionRead,
    WorkflowInstanceCancel,
    WorkflowInstanceCreate,
    WorkflowInstanceDetail,
    WorkflowInstanceListItem,
    WorkflowInstancePage,
    WorkflowInstanceRead,
    WorkflowStepRead,
)
from app.infrastructure.rate_limit import enforceRouteLimit, rateLimitValue
from app.services.messages_zh import MSG_WORKFLOW_APPROVAL_REAL_AUTH_REQUIRED
from app.services.workflow_approval_query import (
    DETAIL_APPROVAL_REAL_AUTH_REQUIRED,
    WorkflowApprovalQueryService,
)
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_service import WorkflowService

router = APIRouter()

# 限流桶作用域（**常量**：URL 路径会随挂载前缀变化 ⇒ 不能拿路径当键）。
SCOPE_SUBMIT_INSTANCE = "workflow:submit-instance"
SCOPE_REQUEST_APPROVAL = "workflow:request-approval"
SCOPE_APPROVAL_DECISION = "workflow:approval-decision"
SCOPE_CANCEL_INSTANCE = "workflow:cancel-instance"

# 列表上限：与前端 Table 的页大小选项（10/20/50/100）对齐，留 200 的余量。
LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 200
DEFINITION_LIST_LIMIT_DEFAULT = 100
DEFINITION_LIST_LIMIT_MAX = 500


def getWorkflowService() -> WorkflowService:
    return WorkflowService()


def getWorkflowApprovalService() -> WorkflowApprovalService:
    return WorkflowApprovalService()


def getWorkflowApprovalQueryService() -> WorkflowApprovalQueryService:
    return WorkflowApprovalQueryService()


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


def _instanceListItem(
    instance: WorkflowInstance, definition: WorkflowDefinition
) -> WorkflowInstanceListItem:
    """实例行 + 定义上下文（页面要显示「哪个业务能力的哪次执行」）。"""

    return WorkflowInstanceListItem(
        id=instance.id,
        definition_id=instance.definition_id,
        domain_code=definition.domain_code,
        capability_code=definition.capability_code,
        definition_version=definition.version,
        status=instance.status,
        idempotency_key=instance.idempotency_key,
        created_by=instance.created_by,
        error_message=instance.error_message,
        created_time=instance.created_time,
        updated_time=instance.updated_time,
    )


@router.get(
    "/instances",
    response_model=WorkflowInstancePage,
    summary="工作流实例列表",
)
async def listInstances(
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: WorkflowService = Depends(getWorkflowService),
    statusFilter: Annotated[
        str | None, Query(alias="status", max_length=32, description="实例状态过滤")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = LIST_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkflowInstancePage:
    """实例列表（新→旧）。

    可见范围与详情接口**逐条一致**：真实 admin 全部；其他人 = 自己提交的
    ∪ 自己作为审批持有人可见的。未验签的非 admin 只剩自作用域。
    """

    rows, total = await service.listInstances(
        session,
        actor=currentUser.userId,
        roles=currentUser.roles,
        isRealIdentity=currentUser.isRealIdentity,
        status=statusFilter,
        limit=limit,
        offset=offset,
    )
    return WorkflowInstancePage(
        rows=[_instanceListItem(instance, definition) for instance, definition in rows],
        total=total,
    )


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


def _approvalListItem(
    task: ApprovalTask,
    instance: WorkflowInstance,
    definition: WorkflowDefinition,
) -> WorkflowApprovalListItem:
    """待办行 = 审批任务 + 实例上下文（页面不必逐行再拉详情）。"""

    return WorkflowApprovalListItem(
        id=task.id,
        instance_id=task.instance_id,
        matrix_code=task.matrix_code,
        subject_type=task.subject_type,
        subject_id=task.subject_id,
        status=task.status,
        approver_role=task.approver_role,
        approver_user=task.approver_user,
        decision_note=task.decision_note,
        due_at=task.due_at,
        decided_at=task.decided_at,
        created_by=task.created_by,
        domain_code=definition.domain_code,
        capability_code=definition.capability_code,
        definition_version=definition.version,
        instance_status=instance.status,
        instance_created_by=instance.created_by,
    )


@router.get(
    "/approvals",
    response_model=WorkflowApprovalPage,
    summary="我的待办审批",
)
async def listMyApprovals(
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    approvalQuery: WorkflowApprovalQueryService = Depends(getWorkflowApprovalQueryService),
    statusFilter: Annotated[
        str | None, Query(alias="status", max_length=32, description="任务状态过滤（默认 PENDING）")
    ] = "PENDING",
    limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = LIST_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkflowApprovalPage:
    """当前用户可审批的任务（默认只看待审批）。

    ⚠️ **要求已校验身份**（``isRealIdentity``）：待办是跨用户读 ⇒ stub 头
    可冒充任意账号 ⇒ 未验签一律 403（与决策面同口径）。
    传 ``status=""`` 可取消状态过滤（跨状态看全部）。
    """

    rows, total = await approvalQuery.listMyApprovals(
        session,
        actor=currentUser.userId,
        roles=currentUser.roles,
        isRealIdentity=currentUser.isRealIdentity,
        status=statusFilter or None,
        limit=limit,
        offset=offset,
    )
    return WorkflowApprovalPage(
        rows=[_approvalListItem(task, instance, definition) for task, instance, definition in rows],
        total=total,
    )


@router.get(
    "/definitions",
    response_model=list[WorkflowDefinitionRead],
    summary="已发布工作流定义列表",
)
async def listDefinitions(
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: WorkflowService = Depends(getWorkflowService),
    limit: Annotated[
        int, Query(ge=1, le=DEFINITION_LIST_LIMIT_MAX)
    ] = DEFINITION_LIST_LIMIT_DEFAULT,
) -> list[WorkflowDefinitionRead]:
    """提交表单的候选集：只回 PUBLISHED，**不含定义体**（审批矩阵里有审批人）。"""

    rows = await service.listPublishedDefinitions(session, limit=limit)
    return [WorkflowDefinitionRead.model_validate(row) for row in rows]


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
            MSG_WORKFLOW_APPROVAL_REAL_AUTH_REQUIRED,
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
