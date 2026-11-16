"""P5 审批决策 → 推进工作流：**两段事务**（决策是事实，推进可失败）。

为什么补这条
------------
W5 评审 HIGH-3：审批接口原来把「决策 + 推进工作流」放在**同一个事务**里
（末尾一次 ``commit``）⇒ 推进阶段一旦抛异常 ⇒ 整个事务回滚 ⇒ **已经发生的
审批被抹掉**（审批人明明批过了，库里还是 ``PENDING``）：

- 审批人要重新批一遍（四眼原则的「一次」语义失真）；
- 审计行也跟着回滚 ⇒ 「谁批的」查无实据；
- 更糟：调用方收到 500 ⇒ 重试 ⇒ 撞上「重复审批 409」⇒ 现场无法解释。

⇒ 契约（本次落地）：

1. **决策先落库**（决策是**事实**：审批人确实批了）；
2. **推进在独立事务**里做（推进失败**不得**回滚已发生的决策）；
3. 推进失败 ⇒ 决策保留 + 实例**失败关闭**（``FAILED`` + 原因）⇒ 没有人会
   误以为工作流还在跑（也不会误以为没批过）。

被测：``WorkflowService.decideApprovalAndAdvance``（API 层只是薄壳）。

⚠️ 本文件走**真实 PostgreSQL**（``unit/`` 目录的强制规则）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
)
from app.models.rbac import Role, User, UserRole
from app.services.workflow_orchestrator import WorkflowOrchestrator
from app.services.workflow_service import WorkflowService
from app.services.workflow_store import WorkflowStore

_APPROVER_ROLE = "purchase_manager"
_CAPABILITY = "po_write"

_MATRIX = [
    {
        "matrixCode": "po_medium_manager",
        "subjectType": "workflow_instance",
        "capabilityCode": _CAPABILITY,
        "riskLevel": "MEDIUM",
        "approverRole": _APPROVER_ROLE,
        "timeoutHours": 24,
    }
]


class _FailingOrchestrator(WorkflowOrchestrator):
    """推进阶段必定失败（模拟编排器/下游故障）。

    只在**审批决策之后**才被调用（本用例不走 ``submitInstance`` 的驱动）。
    """

    async def drive(self, session: AsyncSession, *, instanceId: int):  # noqa: ANN201
        raise RuntimeError("编排器推进失败（测试注入）")


@pytest.fixture()
async def approver(dbSession: AsyncSession) -> User:
    """审批人（真实 DB 用户 + 采购经理角色）。"""

    role = Role(code=_APPROVER_ROLE, name="采购经理")
    user = User(username="lead", display_name="审批主管", enabled=True)
    dbSession.add_all([role, user])
    await dbSession.commit()
    dbSession.add(UserRole(user_id=user.id, role_id=role.id))
    await dbSession.commit()
    return user


@pytest.fixture()
async def definition(dbSession: AsyncSession) -> WorkflowDefinition:
    """一步（既审批又写外部系统）⇒ 审批通过后应入队一条命令。"""

    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code=_CAPABILITY,
        version="v1",
        definition_json={
            "riskLevel": "MEDIUM",
            "steps": [
                {
                    "code": "write",
                    "order": 0,
                    "requiresApproval": True,
                    "commandType": "po.write",
                }
            ],
            "approvalMatrix": _MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _waitingInstance(
    dbSession: AsyncSession, definition: WorkflowDefinition
) -> tuple[WorkflowInstance, ApprovalTask]:
    """造一个「等审批」的实例 + 一个 PENDING 审批任务。"""

    store = WorkflowStore()
    instance, _ = await store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="flow-1",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()
    await store.advanceInstance(dbSession, instance.id, "RUNNING")
    await store.advanceInstance(dbSession, instance.id, "WAITING_APPROVAL")
    await store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[
            {
                "code": "write",
                "order": 0,
                "input": {
                    "commandType": "po.write",
                    "requiresApproval": True,
                    "payload": {},
                },
            }
        ],
    )
    await dbSession.commit()

    task, _created = await store.createApprovalTask(
        dbSession,
        instanceId=instance.id,
        subjectType="workflow_instance",
        subjectId=str(instance.id),
        matrixCode="po_medium_manager",
        approverRole=_APPROVER_ROLE,
        approverUser=None,
        dueAt=datetime.now(UTC) + timedelta(hours=24),
        createdBy="submitter",
    )
    await dbSession.commit()
    return instance, task


async def _taskRow(dbSession: AsyncSession, taskId: int) -> ApprovalTask:
    return (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == taskId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _instanceRow(dbSession: AsyncSession, instanceId: int) -> WorkflowInstance:
    return (
        await dbSession.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.id == instanceId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _commandRows(dbSession: AsyncSession, instanceId: int) -> list[BusinessCommand]:
    rows = (
        (
            await dbSession.execute(
                select(BusinessCommand)
                .where(BusinessCommand.instance_id == instanceId)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# =============================================================================
# 推进失败 ⇒ 决策保留 + 实例失败关闭
# =============================================================================


async def test_推进失败时决策仍然落库(
    dbSession: AsyncSession, definition: WorkflowDefinition, approver: User
):
    """**决策是事实**：推进炸了也不能把审批抹掉。

    回归点：旧实现「决策 + 推进」同事务 ⇒ 推进异常 ⇒ 审批被回滚成 PENDING。
    """

    instance, task = await _waitingInstance(dbSession, definition)
    service = WorkflowService(orchestrator=_FailingOrchestrator())

    await service.decideApprovalAndAdvance(
        dbSession,
        taskId=task.id,
        decision="APPROVE",
        actor="lead",
        roles=(_APPROVER_ROLE,),
        note="同意",
    )

    row = await _taskRow(dbSession, task.id)
    assert row.status == "APPROVED"
    assert row.approver_user == "lead"
    # 没有命令被入队（推进在入队前就炸了 ⇒ 外部写没发生）
    assert await _commandRows(dbSession, instance.id) == []


async def test_推进失败时实例失败关闭(
    dbSession: AsyncSession, definition: WorkflowDefinition, approver: User
):
    """推进失败 ⇒ 实例 ``FAILED``（+ 原因）⇒ 不会「看起来还在跑」。"""

    instance, task = await _waitingInstance(dbSession, definition)
    service = WorkflowService(orchestrator=_FailingOrchestrator())

    await service.decideApprovalAndAdvance(
        dbSession,
        taskId=task.id,
        decision="APPROVE",
        actor="lead",
        roles=(_APPROVER_ROLE,),
    )

    row = await _instanceRow(dbSession, instance.id)
    assert row.status == "FAILED"
    assert row.error_message


async def test_推进失败时不抛异常(
    dbSession: AsyncSession, definition: WorkflowDefinition, approver: User
):
    """推进失败**不应**把整个请求打成 500。

    为什么：审批已经成功 ⇒ 把它报成失败会让调用方重试 ⇒ 重试撞 409
    （重复审批）⇒ 现场无法解释。⇒ 决策成功即返回成功，推进失败走失败关闭。
    """

    _instance, task = await _waitingInstance(dbSession, definition)
    service = WorkflowService(orchestrator=_FailingOrchestrator())

    result = await service.decideApprovalAndAdvance(
        dbSession,
        taskId=task.id,
        decision="APPROVE",
        actor="lead",
        roles=(_APPROVER_ROLE,),
    )

    assert result.status == "APPROVED"


# =============================================================================
# 对照组：推进成功
# =============================================================================


async def test_推进成功时入队命令(
    dbSession: AsyncSession, definition: WorkflowDefinition, approver: User
):
    """正常路径：决策落库 ⇒ 推进入队命令（Worker 领得到活儿）。"""

    instance, task = await _waitingInstance(dbSession, definition)
    service = WorkflowService()

    await service.decideApprovalAndAdvance(
        dbSession,
        taskId=task.id,
        decision="APPROVE",
        actor="lead",
        roles=(_APPROVER_ROLE,),
    )

    commands = await _commandRows(dbSession, instance.id)
    assert len(commands) == 1
    assert commands[0].status == "PENDING"
    row = await _instanceRow(dbSession, instance.id)
    assert row.status == "RUNNING"
