"""P5 审批**超时清扫**单测（独立成文件）。

为什么单独成文件：``test_workflow_approval.py`` 已逼近仓库 800 行硬上限
（单文件 200–400 行为宜，不超过 800 行）⇒ 清扫相关用例集中到这里，
审批文件只留「矩阵解析 / 生成 / 可见性 / 决策」。

被测纪律：

1. **有界**：候选查询带 ``LIMIT``（``SWEEP_BATCH_SIZE``）⇒ 超期任务再多也不会
   把一次事务撑爆（剩下的下一轮再扫）。
2. **重读后再落**：候选是「捞的时候」的快照 ⇒ 落 TIMEOUT 前**重读**（行锁）
   ⇒ 已被并发决策掉的那条**只跳过它**。
3. **逐条 SAVEPOINT**：某一条落库失败（脏数据 / 约束冲突 / 瞬时锁冲突）
   ⇒ **只回滚那一条** ⇒ 其余超期任务照常清扫。
4. **审计写真实前态**：超时审计的 ``before`` 必须是「落 TIMEOUT 前的真实状态」
   ⇒ 审计说的和库里发生的是同一件事。

全程走**真实 PostgreSQL**（``workflow_*`` 表是真表、真事务）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.domain.models import AuditLog
from app.domain.workflow_models import ApprovalTask, WorkflowDefinition
from app.models.rbac import Role
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_approval_store import WorkflowApprovalStore
from app.services.workflow_store import WorkflowStore

_service = WorkflowApprovalService()

_VALID_MATRIX = [
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

# 审计侧的动作 / 实体口径（与 ``workflow_audit_service`` 一致）。
_ACTION_TIMEOUT = "WORKFLOW_TIMEOUT"
_ENTITY_APPROVAL_TASK = "approval_task"


# =============================================================================
# 夹具
# =============================================================================


@pytest.fixture()
async def approverRole(dbSession) -> Role:
    """矩阵里的审批角色必须真实存在（否则生成谁也批不了的任务）。"""

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
            "approvalMatrix": _VALID_MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _requestApproval(dbSession, instance, *, actor="submitter", roles=(), isRealIdentity=False):
    tasks = await _service.requestApproval(
        dbSession,
        instanceId=instance.id,
        actor=actor,
        roles=roles,
        isRealIdentity=isRealIdentity,
    )
    await dbSession.commit()
    return tasks


async def _overdueInstance(dbSession, definition, *, key: str) -> int:
    """建一个「已超期未决」的审批任务，返回任务 id。"""

    store = WorkflowStore()
    instance, _ = await store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey=key,
        requestJson={},
        actor="submitter",
    )
    await dbSession.commit()
    await store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    tasks = await _requestApproval(dbSession, instance)
    task = tasks[0]
    task.due_at = task.due_at - timedelta(days=1)
    await dbSession.commit()
    return task.id


async def _taskStatus(dbSession, taskId: int) -> str:
    row = (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == taskId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.status


# =============================================================================
# 有界 + 并发隔离
# =============================================================================


async def test_清扫有批量上限(dbSession, definition, monkeypatch):
    """清扫**必须有界**：超期任务再多也不能一次全捞进内存与事务。

    回归点：候选查询**没有 LIMIT** ⇒ 一万个超期任务 = 一次事务里一万次
    行锁 + 实例推进 ⇒ 事务无限膨胀 ⇒ 清扫本身把库拖垮。
    """

    from app.services import workflow_approval_service as approvalModule

    for index in range(3):
        await _overdueInstance(dbSession, definition, key=f"sweep-limit-{index}")

    monkeypatch.setattr(approvalModule, "SWEEP_BATCH_SIZE", 2)

    swept = await _service.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()

    assert swept == 2
    pending = (
        (
            await dbSession.execute(
                select(ApprovalTask).where(ApprovalTask.status == "PENDING")
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 1


async def test_并发决策不得拖垮整批清扫(dbSession, definition, monkeypatch):
    """某条任务在「捞候选」与「落 TIMEOUT」之间被并发决策 ⇒ **只跳过这一条**。

    回归点：清扫用的是**捞候选时**的旧状态 ⇒ 落 TIMEOUT 时状态机判定
    ``APPROVED → TIMEOUT`` 非法 ⇒ 抛 ConflictError ⇒ **整批回滚**
    ⇒ 一次并发决策就废掉本轮所有超期任务。
    """

    firstId = await _overdueInstance(dbSession, definition, key="sweep-race-a")
    secondId = await _overdueInstance(dbSession, definition, key="sweep-race-b")

    originalLockInstance = WorkflowApprovalService._lockInstance
    state = {"flipped": False}

    async def _patchedLockInstance(self, session, instanceId):
        if not state["flipped"]:
            state["flipped"] = True
            # 模拟并发：另一个会话已把这条审批批了（已提交）
            await session.execute(
                update(ApprovalTask)
                .where(
                    ApprovalTask.id == firstId,
                    ApprovalTask.status == "PENDING",
                )
                .values(status="APPROVED", approver_user="manager")
            )
            await session.flush()
        return await originalLockInstance(self, session, instanceId)

    monkeypatch.setattr(WorkflowApprovalService, "_lockInstance", _patchedLockInstance)

    swept = await _service.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()

    assert swept == 1
    rows = {
        row.id: row.status
        for row in (
            (await dbSession.execute(select(ApprovalTask))).scalars().all()
        )
    }
    assert rows[firstId] == "APPROVED"
    assert rows[secondId] == "TIMEOUT"


# =============================================================================
# R12-M4：逐条 SAVEPOINT ⇒ 单条失败只回滚那一条
# =============================================================================


async def test_单条落库失败只回滚那一条(dbSession, definition, monkeypatch):
    """一条超期任务落库失败 ⇒ **只回滚这一条**，其余照常清扫。

    回归点：整批共用一个事务 ⇒ 一条脏数据就把本轮**所有**超期任务全部作废
    （下一轮再来）⇒ 一条脏数据能无限期地让整批超期任务不落库
    ⇒ 实例永远卡在「等待审批」。

    注入点选在**存储层**（``markApprovalTask``）而不是服务层 ⇒ 被测的是真实
    的「状态推进 → 写事件 → 落审计」整条路径，而不是一个空壳。
    """

    boomId = await _overdueInstance(dbSession, definition, key="sweep-boom")
    goodId = await _overdueInstance(dbSession, definition, key="sweep-good")

    originalMark = WorkflowApprovalStore.markApprovalTask

    async def _patchedMark(self, session, taskId, **kwargs):
        if taskId == boomId:
            raise RuntimeError("脏数据：该审批任务落 TIMEOUT 失败")
        return await originalMark(self, session, taskId, **kwargs)

    monkeypatch.setattr(WorkflowApprovalStore, "markApprovalTask", _patchedMark)

    swept = await _service.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()

    # 失败的那条**没落库**（SAVEPOINT 回滚 ⇒ 仍是 PENDING）。
    assert swept == 1
    assert await _taskStatus(dbSession, boomId) == "PENDING"
    # 成功的那条**照常落库**（隔离生效）。
    assert await _taskStatus(dbSession, goodId) == "TIMEOUT"


# =============================================================================
# R12-L2：超时审计记录**变更前的真实状态**
# =============================================================================


async def test_超时审计记录变更前的真实状态(dbSession, definition):
    """超时审计的 ``before`` 必须是「落 TIMEOUT 前的真实状态」。

    ⚠️ **口径说明**：审批矩阵里 ``TIMEOUT`` 只能来自 ``PENDING``
    ⇒ 今天的真实前态**恰好**就是 ``PENDING`` ⇒ 本用例钉的是**契约**
    （审计说的 = 库里发生的那件事），不是某个具体实现的差别。
    真正会跑偏的是**补偿**审计（实例补偿前可能是 ``RUNNING``）
    ⇒ 那条另在 ``test_workflow_compensation.py`` 里钉。
    """

    taskId = await _overdueInstance(dbSession, definition, key="sweep-audit")
    priorStatus = await _taskStatus(dbSession, taskId)
    assert priorStatus == "PENDING"

    swept = await _service.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()
    assert swept == 1

    rows = (
        (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == _ENTITY_APPROVAL_TASK,
                    AuditLog.entity_id == taskId,
                    AuditLog.action == _ACTION_TIMEOUT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].before_json == {"status": priorStatus}
    assert rows[0].after_json == {"status": "TIMEOUT"}
