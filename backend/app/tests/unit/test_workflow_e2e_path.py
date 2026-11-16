"""P5 **全路径**端到端用例（真实 PostgreSQL，跨模块接力）。

G6 判据要求「状态机全路径」可走通。本文件钉的是**跨模块接力**（不是单模块）：

    提交 → 命令入队 → Worker 执行 → 步骤 SUCCEEDED → 编排器推进 →
    自动发起审批 → 审批通过 → 审批门步骤完成 → 实例 COMPLETED

单模块用例各占一个文件（编排器 / 执行器 / 审批 / 取消）⇒ **接不上**：
「上一棒跑完下一棒没人接手」这类断链只能靠全路径用例抓出来。

被测路径：

1. **全路径成功**：命令执行 ⇒ 自动发起审批 ⇒ 审批通过 ⇒ 实例 COMPLETED；
2. **审批拒绝**：实例 REJECTED，**后续命令不入队**（不写外部系统）；
3. **审批超时**：清扫 ⇒ 实例 FAILED（不死等）；
4. **重复提交**：同幂等键 ⇒ 同一实例、**命令仍只有一条**（不重复写）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.models.rbac import Role
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import CommandHandlerRegistry, SimulatedCommandHandler
from app.services.workflow_orchestrator import WorkflowOrchestrator
from app.services.workflow_service import WorkflowService

_service = WorkflowService()
_orchestrator = WorkflowOrchestrator()
_approval = WorkflowApprovalService()

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

# 「先执行一条命令，再过一道审批门」的两步定义。
_STEPS = [
    {"code": "collect", "order": 0, "commandType": "po.collect"},
    {"code": "gate", "order": 1, "requiresApproval": True},
]


@pytest.fixture()
async def approverRole(dbSession: AsyncSession) -> Role:
    row = Role(code=_APPROVER_ROLE, name="采购经理")
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _seedDefinition(dbSession: AsyncSession) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code=_CAPABILITY,
        version="v1",
        definition_json={
            "riskLevel": "MEDIUM",
            "steps": _STEPS,
            "approvalMatrix": _MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


def _executor() -> CommandExecutor:
    registry = CommandHandlerRegistry()
    registry.register("po.collect", SimulatedCommandHandler())
    return CommandExecutor(registry, maxAttempts=3)


async def _rows(dbSession: AsyncSession, model, instanceId: int) -> list:
    rows = (
        (
            await dbSession.execute(
                select(model).where(model.instance_id == instanceId).order_by(model.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _instanceRow(dbSession: AsyncSession, instanceId: int) -> WorkflowInstance:
    return (
        await dbSession.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.id == instanceId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# =============================================================================
# 全路径
# =============================================================================


async def test_全路径_提交到实例完成(dbSession: AsyncSession, approverRole):
    """提交 → 命令 → 执行 → 自动审批 → 通过 → 实例 COMPLETED。"""

    definition = await _seedDefinition(dbSession)

    instance, created = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="e2e-1",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()
    instanceId = instance.id
    assert created is True

    # ① 提交即入队首条命令（命令的来路）
    commands = await _rows(dbSession, BusinessCommand, instanceId)
    assert len(commands) == 1
    assert commands[0].command_type == "po.collect"
    assert commands[0].status == "PENDING"

    # ② Worker 执行命令 ⇒ 步骤 SUCCEEDED ⇒ 编排器推进到审批门 ⇒ 自动发起审批
    outcome = await _executor().execute(dbSession, commands[0].id, workerId="worker-1")
    assert outcome.status == "SUCCEEDED"

    steps = await _rows(dbSession, WorkflowStep, instanceId)
    assert steps[0].status == "SUCCEEDED"
    approvals = await _rows(dbSession, ApprovalTask, instanceId)
    assert len(approvals) == 1
    assert approvals[0].status == "PENDING"
    row = await _instanceRow(dbSession, instanceId)
    assert row.status == "WAITING_APPROVAL"

    # ③ 审批通过 ⇒ 编排器把审批门步骤标记完成 ⇒ 全部步骤完成 ⇒ 实例 COMPLETED
    await _approval.decideApproval(
        dbSession,
        taskId=approvals[0].id,
        decision="APPROVE",
        actor="manager",
        roles=[_APPROVER_ROLE],
    )
    await dbSession.commit()
    await _orchestrator.drive(dbSession, instanceId=instanceId)
    await dbSession.commit()

    steps = await _rows(dbSession, WorkflowStep, instanceId)
    assert [s.status for s in steps] == ["SUCCEEDED", "SUCCEEDED"]
    row = await _instanceRow(dbSession, instanceId)
    assert row.status == "COMPLETED"


async def test_全路径_审批拒绝则命令不再入队(dbSession: AsyncSession, approverRole):
    """拒绝 ⇒ 实例 REJECTED，后续步骤的命令**不入队**（外部写不再发生）。"""

    definition = await _seedDefinition(dbSession)

    instance, _ = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="e2e-2",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()
    instanceId = instance.id

    commands = await _rows(dbSession, BusinessCommand, instanceId)
    await _executor().execute(dbSession, commands[0].id, workerId="worker-1")

    approvals = await _rows(dbSession, ApprovalTask, instanceId)
    await _approval.decideApproval(
        dbSession,
        taskId=approvals[0].id,
        decision="REJECT",
        actor="manager",
        roles=[_APPROVER_ROLE],
    )
    await dbSession.commit()
    await _orchestrator.drive(dbSession, instanceId=instanceId)
    await dbSession.commit()

    row = await _instanceRow(dbSession, instanceId)
    assert row.status == "REJECTED"
    # 只有「采集」那一条命令（审批门本身没有命令）
    commands = await _rows(dbSession, BusinessCommand, instanceId)
    assert [c.command_type for c in commands] == ["po.collect"]


async def test_全路径_审批超时则实例失败(dbSession: AsyncSession, approverRole):
    """超期未决 ⇒ 清扫 ⇒ 实例 FAILED（不死等）。"""

    definition = await _seedDefinition(dbSession)

    instance, _ = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="e2e-3",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()
    instanceId = instance.id

    commands = await _rows(dbSession, BusinessCommand, instanceId)
    await _executor().execute(dbSession, commands[0].id, workerId="worker-1")

    approvals = await _rows(dbSession, ApprovalTask, instanceId)
    task = approvals[0]
    task.due_at = task.due_at.replace(year=task.due_at.year - 1)
    await dbSession.commit()

    swept = await _approval.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()

    assert swept == 1
    row = await _instanceRow(dbSession, instanceId)
    assert row.status == "FAILED"


async def test_全路径_重复提交不重复入队(dbSession: AsyncSession, approverRole):
    """同幂等键重复提交 ⇒ 同一实例、**命令仍只有一条**（不重复写外部系统）。"""

    definition = await _seedDefinition(dbSession)

    instance, created = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="e2e-4",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()
    instanceId = instance.id

    replay, replayCreated = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="e2e-4",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()

    assert created is True
    assert replayCreated is False
    assert replay.id == instanceId
    commands = await _rows(dbSession, BusinessCommand, instanceId)
    assert len(commands) == 1
