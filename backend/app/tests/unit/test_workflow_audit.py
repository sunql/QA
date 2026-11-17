"""P5 审计接入：**每步可追溯**（事件流）+ **治理动作**（audit_log）。

W4 完成定义：「审计接入（每步可追溯）」。

两条审计面（刻意分开，职责不同）：

1. ``workflow_outbox``（P5 自有 outbox，D-5）：**每步可追溯** —— 领域事件流。
   实例创建 / 步骤创建 / 状态流转 / 命令请求 / 审批请求 / 审批决策 / 超时
   全部在**同一事务**里入队（只 add 不 commit）⇒ 与业务写原子同生共死。
   它是「发生了什么」的事实流。
2. ``audit_log``（既有治理表）：**谁做了什么** —— 提交 / 发起审批 / 通过 /
   拒绝 / 超时 / 补偿。动作走 ``AuditService._VALID_ACTIONS`` 白名单
   （本批扩了 P5 的 6 个动作；DB CHECK 约束同步放宽，见迁移 0152）。

⚠️ 断言口径：断**行落在真实 PG 上**（动作、实体、actor、前后快照、事件类型），
不只看「函数被调用过」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.models import AuditLog
from app.domain.workflow_models import ApprovalTask, WorkflowDefinition, WorkflowOutbox
from app.models.rbac import Role
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_service import WorkflowService

_MATRIX = [
    {
        "matrixCode": "po_medium_manager",
        "subjectType": "workflow_instance",
        "capabilityCode": "purchase_order_approve",
        "riskLevel": "MEDIUM",
        "approverRole": "purchase_manager",
        "timeoutHours": 24,
    }
]

_APPROVER_ROLE_CODE = "purchase_manager"


@pytest.fixture()
async def approverRole(dbSession) -> Role:
    row = Role(code=_APPROVER_ROLE_CODE, name="采购经理")
    dbSession.add(row)
    await dbSession.commit()
    return row


@pytest.fixture()
async def definition(dbSession, approverRole) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="purchase_order_approve",
        version="v1",
        definition_json={
            "riskLevel": "MEDIUM",
            "steps": [
                {"code": "collect", "order": 0, "commandType": "po.collect"},
                {"code": "approve", "order": 1, "requiresApproval": True},
            ],
            "approvalMatrix": _MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _submit(dbSession, definition) -> int:
    service = WorkflowService()
    instance, created = await service.submitInstance(
        dbSession,
        domainCode="procurement",
        capabilityCode="purchase_order_approve",
        version="v1",
        idempotencyKey="audit-key-1",
        requestJson={"amount": 100},
        actor="submitter",
    )
    await dbSession.commit()
    assert created
    return instance.id


async def _auditRows(dbSession, *, entityType: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog)
    if entityType is not None:
        stmt = stmt.where(AuditLog.entity_type == entityType)
    rows = (await dbSession.execute(stmt)).scalars().all()
    return list(rows)


async def _outboxEvents(dbSession, instanceId: int) -> list[str]:
    rows = (
        (
            await dbSession.execute(
                select(WorkflowOutbox).where(WorkflowOutbox.instance_id == instanceId)
            )
        )
        .scalars()
        .all()
    )
    return [row.event_type for row in rows]


# =============================================================================
# 治理审计（audit_log）
# =============================================================================


async def test_提交实例记审计(dbSession, definition):
    instanceId = await _submit(dbSession, definition)

    rows = await _auditRows(dbSession, entityType="workflow_instance")
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "WORKFLOW_SUBMIT"
    assert row.entity_id == instanceId
    assert row.actor == "submitter"


async def test_发起审批记审计(dbSession, definition):
    instanceId = await _submit(dbSession, definition)
    approval = WorkflowApprovalService()

    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    rows = await _auditRows(dbSession, entityType="approval_task")
    assert len(rows) == 1
    assert rows[0].action == "WORKFLOW_APPROVAL_REQUESTED"
    assert rows[0].actor == "submitter"


async def test_审批通过记APPROVE(dbSession, definition):
    instanceId = await _submit(dbSession, definition)
    approval = WorkflowApprovalService()
    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    task = (
        await dbSession.execute(
            select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
        )
    ).scalar_one()
    taskId = task.id

    await approval.decideApproval(
        dbSession,
        taskId=taskId,
        decision="APPROVE",
        actor="manager",
        roles=(_APPROVER_ROLE_CODE,),
        note="同意",
    )
    await dbSession.commit()

    rows = await _auditRows(dbSession, entityType="approval_task")
    actions = [row.action for row in rows]
    assert "APPROVE" in actions
    approved = next(row for row in rows if row.action == "APPROVE")
    assert approved.actor == "manager"
    assert approved.entity_id == taskId
    assert approved.after_json["status"] == "APPROVED"
    assert approved.after_json["approverUser"] == "manager"


async def test_审批拒绝记REJECT(dbSession, definition):
    instanceId = await _submit(dbSession, definition)
    approval = WorkflowApprovalService()
    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    task = (
        await dbSession.execute(
            select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
        )
    ).scalar_one()
    taskId = task.id

    await approval.decideApproval(
        dbSession,
        taskId=taskId,
        decision="REJECT",
        actor="manager",
        roles=(_APPROVER_ROLE_CODE,),
        note="预算不足",
    )
    await dbSession.commit()

    rows = await _auditRows(dbSession, entityType="approval_task")
    actions = [row.action for row in rows]
    assert "REJECT" in actions


async def test_超时记WORKFLOW_TIMEOUT(dbSession, definition):
    instanceId = await _submit(dbSession, definition)
    approval = WorkflowApprovalService()
    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    task = (
        await dbSession.execute(
            select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
        )
    ).scalar_one()
    task.due_at = task.due_at.replace(year=task.due_at.year - 1)
    await dbSession.commit()

    swept = await approval.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()
    assert swept == 1

    rows = await _auditRows(dbSession, entityType="approval_task")
    actions = [row.action for row in rows]
    assert "WORKFLOW_TIMEOUT" in actions


# =============================================================================
# 每步可追溯（workflow_outbox 事件流）
# =============================================================================


async def test_提交实例留事件流(dbSession, definition):
    instanceId = await _submit(dbSession, definition)

    events = await _outboxEvents(dbSession, instanceId)
    assert "instance_created" in events
    assert "steps_created" in events
    assert "instance_status_changed" in events


async def test_推进步骤留事件流(dbSession, definition):
    from app.domain.workflow_models import WorkflowStep
    from app.services.workflow_store import WorkflowStore

    instanceId = await _submit(dbSession, definition)
    store = WorkflowStore()
    # 定义里有 2 个步骤 ⇒ 取第一个（不是唯一）。
    step = (
        (
            await dbSession.execute(
                select(WorkflowStep)
                .where(WorkflowStep.instance_id == instanceId)
                .order_by(WorkflowStep.step_order, WorkflowStep.id)
            )
        )
        .scalars()
        .first()
    )
    assert step is not None

    await store.advanceStep(dbSession, step.id, "RUNNING")
    await dbSession.commit()

    events = await _outboxEvents(dbSession, instanceId)
    assert "step_status_changed" in events


async def test_审批决策留事件流(dbSession, definition):
    instanceId = await _submit(dbSession, definition)
    approval = WorkflowApprovalService()
    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    task = (
        await dbSession.execute(
            select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
        )
    ).scalar_one()
    taskId = task.id

    await approval.decideApproval(
        dbSession,
        taskId=taskId,
        decision="APPROVE",
        actor="manager",
        roles=(_APPROVER_ROLE_CODE,),
    )
    await dbSession.commit()

    events = await _outboxEvents(dbSession, instanceId)
    assert "approval_requested" in events
    assert "approval_decided" in events


class _FakeSession:
    """最小 session 替身：只断言「入队不 commit」。"""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = False

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True


async def test_入队不自带commit():
    """``enqueueEvent`` 只 add 不 commit（同事务语义：业务回滚 ⇒ 事件也回滚）。"""

    from app.services.workflow_store import WorkflowStore

    fake = _FakeSession()
    await WorkflowStore().enqueueEvent(
        fake,
        instanceId=None,
        eventType="instance_created",
        payload={"probe": True},
    )

    assert len(fake.added) == 1
    assert fake.committed is False


# =============================================================================
# R11-I / R11-J：审计快照必须反映「决策前」；未新建的任务不重复留痕
# =============================================================================


async def test_审批审计的before快照是决策前(dbSession, definition):
    """``before_json`` 必须是**决策前**的快照。

    回归点：``recordDecision`` 拿的是**更新后**的任务行来填 ``before``
    ⇒ ``before.approverUser`` 显示的其实是**新**审批人 ⇒ 「变更前」快照里
    出现了变更后的值 ⇒ 审计的 before/after 两端不再是同一件事的前后。
    """

    instanceId = await _submit(dbSession, definition)
    approval = WorkflowApprovalService()
    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    task = (
        await dbSession.execute(
            select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
        )
    ).scalar_one()
    taskId = task.id

    await approval.decideApproval(
        dbSession,
        taskId=taskId,
        decision="APPROVE",
        actor="manager",
        roles=(_APPROVER_ROLE_CODE,),
        note="同意",
    )
    await dbSession.commit()

    rows = await _auditRows(dbSession, entityType="approval_task")
    approved = next(row for row in rows if row.action == "APPROVE")
    assert approved.before_json["status"] == "PENDING"
    # 决策前还没有签署人
    assert approved.before_json["approverUser"] is None
    assert approved.after_json["approverUser"] == "manager"


async def test_审批任务未新建时不重复留审计(dbSession, definition, monkeypatch):
    """并发窗口：任务**已存在**（别人先插成功）⇒ 本次**未新建** ⇒ 不重复留痕。

    回归点：``_createRoundTasks`` 忽略了 ``createApprovalTask`` 返回的
    ``created`` 标志 ⇒ 未新建也写一条 ``WORKFLOW_APPROVAL_REQUESTED``
    ⇒ 审计里出现「同一任务被发起两次」，且第二次的 actor 是并发的那一方。
    """

    from datetime import UTC, datetime, timedelta

    from app.services.workflow_store import WorkflowStore

    instanceId = await _submit(dbSession, definition)
    store = WorkflowStore()
    await store.createApprovalTask(
        dbSession,
        instanceId=instanceId,
        subjectType="workflow_instance",
        subjectId=str(instanceId),
        matrixCode="po_medium_manager",
        approverRole="purchase_manager",
        approverUser=None,
        dueAt=datetime.now(UTC) + timedelta(hours=24),
        createdBy="other",
    )
    await dbSession.commit()

    async def _emptyList(session, *, instanceId):
        # 模拟竞态窗口：检查时列表为空 ⇒ 走「创建」路径 ⇒ 唯一约束命中 ⇒ 未新建
        return []

    monkeypatch.setattr(store, "listApprovalTasks", _emptyList)
    approval = WorkflowApprovalService(store=store)

    await approval.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.commit()

    rows = await _auditRows(dbSession, entityType="approval_task")
    assert rows == []
