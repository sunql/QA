"""P5 取消路径 + 幂等键归属（真实 PostgreSQL）。

为什么补这两条
--------------
1109 契约 §6 P5 的验收口径是「**状态机全路径**（含重复提交、超时、取消、补偿）」。
W5 评审发现两条路径在生产上**不可达**：

- **取消**：状态机里有 ``CANCELLED``（实例 / 审批任务），但**没有任何触发者**
  ⇒ 验收口径里的「取消」落空；且待执行的命令**仍会被 Worker 领走**
  （命令状态机里没有「已取消」这一档 ⇒ 取消后仍会真的写外部系统）。
- **幂等键归属**：``submitInstance`` 的幂等重放**不校验 actor** ⇒ 别人复用
  同一个 ``idempotencyKey`` 会**拿到别人的实例**（越权读）。

被测纪律：

1. 取消 ⇒ 实例 CANCELLED；待审批任务 CANCELLED；待执行步骤 SKIPPED；
   待执行命令 CANCELLED（**不再被 Worker 领取**）。
2. 取消权限：只有**提交人**或 admin 能取消（其它人 **404**，不泄露存在性）；
   取消**必须**是**已校验身份**（JWT）—— stub 缺省角色带 admin ⇒ 只看角色
   会被伪造头绕过；且 stub 头可冒充任意 DB 账号 ⇒ 治理写面必须验签。
3. 已终态的实例不能取消（409）；重复取消是幂等的（不报错）。
4. 幂等键是**提交人私有**的：他人复用 ⇒ 409（不泄露别人的实例）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.models.rbac import Role
from app.services.workflow_service import WorkflowService
from app.services.workflow_store import WorkflowStore

_service = WorkflowService()
_store = WorkflowStore()

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


@pytest.fixture()
async def approverRole(dbSession: AsyncSession) -> Role:
    row = Role(code=_APPROVER_ROLE, name="采购经理")
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _seedDefinition(
    dbSession: AsyncSession,
    steps: list[dict],
    *,
    matrix: list[dict] | None = None,
) -> WorkflowDefinition:
    payload: dict = {"steps": steps}
    if matrix is not None:
        payload["riskLevel"] = "MEDIUM"
        payload["approvalMatrix"] = matrix
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code=_CAPABILITY,
        version="v1",
        definition_json=payload,
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _submit(
    dbSession: AsyncSession,
    definition: WorkflowDefinition,
    *,
    idempotencyKey: str = "cancel-1",
    actor: str = "submitter",
) -> WorkflowInstance:
    instance, _created = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey=idempotencyKey,
        requestJson={"amount": 100},
        actor=actor,
    )
    await dbSession.commit()
    return instance


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


# =============================================================================
# 取消
# =============================================================================


async def test_取消实例会取消待执行命令(dbSession: AsyncSession):
    """取消 ⇒ 待执行命令进「已取消」终态 ⇒ Worker 不会再领它。"""

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)
    commands = await _rows(dbSession, BusinessCommand, instance.id)
    assert len(commands) == 1
    assert commands[0].status == "PENDING"

    await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.commit()

    assert instance.status == "CANCELLED"
    commands = await _rows(dbSession, BusinessCommand, instance.id)
    assert commands[0].status == "CANCELLED"
    steps = await _rows(dbSession, WorkflowStep, instance.id)
    assert steps[0].status == "SKIPPED"


async def test_取消后命令不再被领取(dbSession: AsyncSession):
    """取消 ⇒ 命令不可领取（否则取消只是纸面动作，外部系统照写）。"""

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)

    await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.commit()

    claimed = await _store.claimNextCommand(dbSession, workerId="worker-1", maxAttempts=3)

    assert claimed is None


async def test_取消会取消待审批任务(dbSession: AsyncSession, approverRole):
    definition = await _seedDefinition(
        dbSession,
        [{"code": "approve", "order": 0, "requiresApproval": True, "commandType": "po.write"}],
        matrix=_MATRIX,
    )
    instance = await _submit(dbSession, definition)
    approvals = await _rows(dbSession, ApprovalTask, instance.id)
    assert len(approvals) == 1
    assert approvals[0].status == "PENDING"

    await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.commit()

    approvals = await _rows(dbSession, ApprovalTask, instance.id)
    assert approvals[0].status == "CANCELLED"
    assert instance.status == "CANCELLED"


async def test_取消会连带取消待重试的失败命令(dbSession: AsyncSession):
    """可重试失败（``FAILED``）的命令**也在可领取集合里** ⇒ 取消必须一并取消它。

    回归点：旧实现只取消 ``PENDING`` ⇒ ``FAILED`` 的命令取消后仍被 Worker
    领走 ⇒ 「取消 = 不再写外部系统」在该路径上失真。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)
    command = (await _rows(dbSession, BusinessCommand, instance.id))[0]
    commandId = command.id

    # 模拟一次可重试失败（FAILED 且 attempts 未耗尽 ⇒ 仍可被领取）
    await _store.failCommand(dbSession, commandId, attempts=1, errorMessage="下游超时")
    await dbSession.commit()

    await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.commit()

    row = (
        await dbSession.execute(
            select(BusinessCommand).where(BusinessCommand.id == commandId)
        )
    ).scalar_one()
    assert row.status == "CANCELLED"

    claimed = await _store.claimNextCommand(dbSession, workerId="worker-1", maxAttempts=3)
    assert claimed is None


async def test_命令在飞时取消失败关闭(dbSession: AsyncSession):
    """命令在飞（``EXECUTING``）⇒ 取消**失败关闭**（409）。

    为什么：取消与在飞的外部写同时发生 ⇒ 治理面承诺（「取消后不再写外部系统」）
    与实际行为不符（旧实现直接把实例判成 CANCELLED，而外部写照常发生、
    步骤永久悬在 RUNNING）。

    ⇒ 失败关闭：等命令跑完再取消（取消是幂等的，可重试）。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)
    command = (await _rows(dbSession, BusinessCommand, instance.id))[0]
    instanceId = instance.id

    claimed = await _store.claimCommand(
        dbSession, command.id, workerId="worker-1", maxAttempts=3
    )
    assert claimed is not None
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _service.cancelInstance(dbSession, instanceId=instanceId, actor="submitter", isRealIdentity=True)
    await dbSession.rollback()

    row = (
        await dbSession.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
    ).scalar_one()
    assert row.status == "RUNNING"


async def test_非提交人不能取消(dbSession: AsyncSession):
    """无权取消 ⇒ **404**（与「实例不存在」同码 ⇒ 不泄露存在性）。"""

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)
    instanceId = instance.id

    with pytest.raises(NotFoundError):
        await _service.cancelInstance(
            dbSession, instanceId=instanceId, actor="intruder", isRealIdentity=True
        )
    await dbSession.rollback()

    # ⚠️ rollback 会 expire 全部 ORM 实例 ⇒ 重读（不能碰旧对象的属性）
    row = (
        await dbSession.execute(
            select(WorkflowInstance).where(WorkflowInstance.id == instanceId)
        )
    ).scalar_one()
    assert row.status == "RUNNING"


async def test_未校验身份不能取消(dbSession: AsyncSession):
    """**未校验身份**（stub 头）⇒ 403（治理写面必须验签）。

    回归点：旧实现只看 ``created_by == actor`` ⇒ 任意客户端发
    ``X-User-Id: submitter`` 就能取消**别人**的实例 ⇒ 治理面被绕过。
    ⇒ 取消要求 ``isRealIdentity``（JWT 验签过 ⇒ ``jti`` 非空）。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)
    instanceId = instance.id

    with pytest.raises(PermissionDeniedError):
        await _service.cancelInstance(
            dbSession, instanceId=instanceId, actor="submitter", isRealIdentity=False
        )
    await dbSession.rollback()

    row = (
        await dbSession.execute(
            select(WorkflowInstance).where(WorkflowInstance.id == instanceId)
        )
    ).scalar_one()
    assert row.status == "RUNNING"


async def test_管理员可以取消(dbSession: AsyncSession):
    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)

    await _service.cancelInstance(
        dbSession,
        instanceId=instance.id,
        actor="root",
        roles=("admin",),
        dbUserId=7,
        isRealIdentity=True,
    )
    await dbSession.commit()

    assert instance.status == "CANCELLED"


async def test_未校验身份即使带admin角色也不能取消(dbSession: AsyncSession):
    """**跨用户**取消必须是**已校验**的 admin 身份。

    为什么：stub 身份的缺省角色带 ``admin``（``DEFAULT_STUB_ROLES``）⇒
    只看角色的话，**任何**伪造头都能取消别人的实例 ⇒ 治理面被绕过。
    ⇒ 身份闸在权限判定**之前**：未校验身份一律 403（与实例存不存在无关 ⇒
    不构成存在性预言机）。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)
    instanceId = instance.id

    with pytest.raises(PermissionDeniedError):
        await _service.cancelInstance(
            dbSession, instanceId=instanceId, actor="root", roles=("admin",)
        )
    await dbSession.rollback()


async def test_已完成的实例不能取消(dbSession: AsyncSession):
    definition = await _seedDefinition(dbSession, [])
    instance = await _submit(dbSession, definition)
    assert instance.status == "COMPLETED"

    with pytest.raises(ConflictError):
        await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.rollback()


async def test_重复取消是幂等的(dbSession: AsyncSession):
    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition)

    await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.commit()
    await _service.cancelInstance(dbSession, instanceId=instance.id, actor="submitter", isRealIdentity=True)
    await dbSession.commit()

    assert instance.status == "CANCELLED"


async def test_取消不存在的实例报404(dbSession: AsyncSession):
    with pytest.raises(NotFoundError):
        await _service.cancelInstance(dbSession, instanceId=999999, actor="submitter", isRealIdentity=True)
    await dbSession.rollback()


# =============================================================================
# 幂等键归属
# =============================================================================


async def test_他人复用幂等键得到自己的实例(dbSession: AsyncSession):
    """幂等键**按提交人私有**：他人复用同一个键 ⇒ 各自独立实例（不是 409）。

    为什么（旧实现是 409）：409 等于告诉对方「这个键已经有人用了」⇒
    ① 键空间的**存在性预言机**；② 任何人可以**占死**别人的键（DoS）。
    ⇒ 约束作用域含 ``created_by`` ⇒ 键空间按人隔离，各自幂等。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    alice = await _submit(dbSession, definition, idempotencyKey="shared-key", actor="alice")

    bob, created = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="shared-key",
        requestJson={"amount": 100},
        actor="bob",
    )
    await dbSession.commit()

    assert created is True
    assert bob.id != alice.id
    assert bob.created_by == "bob"


async def test_本人复用幂等键仍是幂等重放(dbSession: AsyncSession):
    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    instance = await _submit(dbSession, definition, idempotencyKey="own-key", actor="alice")

    replay, created = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey="own-key",
        requestJson={"amount": 100},
        actor="alice",
    )
    await dbSession.commit()

    assert created is False
    assert replay.id == instance.id
