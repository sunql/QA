"""P5 审批面：定义契约解析 + 审批运行时（真实 PostgreSQL）。

被测纪律（W2）：

1. **审批矩阵是数据**（D-1）：矩阵住在 ``workflow_definition.definition_json``
   ⇒ 改审批规则**只改数据不改代码**。
2. **矩阵键固定三段**：``(subject_type, capability_code, risk_level)``。
   选不中 ⇒ **失败关闭**（拒绝发起审批），绝不「匹配不到就放行」。
3. **风险级别服务端绑定**：来自定义体顶层 ``riskLevel``（**客户端不得自选**
   —— 自选 = 自选审批层级 = 挑低风险绕过高层审批）。
4. **矩阵条目必须有审批人**：既无 ``approverRole`` 又无 ``approverUser``
   ⇒ 解析期拒绝（否则生成一条**谁也批不了**的审批任务 = 死锁）。
5. **审批人必须在库中存在**：角色 / 指定人查无 ⇒ 发起时拒（否则死任务）。
6. **七守卫**：
   - 对象级不可见（404）：无权者看不到实例（无存在性预言机）；
   - 越权（403）：矩阵判据（指定人优先，其次角色）；
   - 四眼（403）：决策人 ≠ 实例提交人 **且** ≠ 本轮发起人；
   - 重复审批（409）：已决过 ⇒ 拒；
   - 超时（409）：过了 ``due_at`` ⇒ 任务置 TIMEOUT、实例置 FAILED。
7. **单轮**：一个实例只允许一轮审批（不叠加）。

矩阵解析是纯函数（无 IO）⇒ 直接单测；审批运行时走**真实 PG**。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.domain.workflow_models import ApprovalTask, WorkflowDefinition
from app.models.rbac import Role, User
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_definition import (
    parseApprovalMatrix,
    parseDefinitionRiskLevel,
    parseWorkflowSteps,
    selectMatrixEntries,
)

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


# =============================================================================
# 定义契约解析（纯函数）
# =============================================================================


def test_解析审批矩阵():
    entries = parseApprovalMatrix({"approvalMatrix": _VALID_MATRIX})

    assert len(entries) == 1
    entry = entries[0]
    assert entry.matrixCode == "po_medium_manager"
    assert entry.subjectType == "workflow_instance"
    assert entry.capabilityCode == "purchase_order_approve"
    assert entry.riskLevel == "MEDIUM"
    assert entry.approverRole == "purchase_manager"
    assert entry.approverUser is None
    assert entry.timeoutHours == 24


def test_矩阵缺审批人则拒绝():
    """既无 approverRole 又无 approverUser ⇒ 解析期拒绝。"""

    bad = {**_VALID_MATRIX[0]}
    bad.pop("approverRole")

    with pytest.raises(ValidationError):
        parseApprovalMatrix({"approvalMatrix": [bad]})


def test_矩阵缺必填字段则拒绝():
    for field in ("matrixCode", "subjectType", "capabilityCode", "riskLevel"):
        bad = {k: v for k, v in _VALID_MATRIX[0].items() if k != field}
        with pytest.raises(ValidationError):
            parseApprovalMatrix({"approvalMatrix": [bad]})


def test_矩阵编码超长则拒绝():
    bad = {**_VALID_MATRIX[0], "matrixCode": "x" * 65}

    with pytest.raises(ValidationError):
        parseApprovalMatrix({"approvalMatrix": [bad]})


def test_超时小时越界则拒绝():
    for hours in (0, -1, 721):
        bad = {**_VALID_MATRIX[0], "timeoutHours": hours}
        with pytest.raises(ValidationError):
            parseApprovalMatrix({"approvalMatrix": [bad]})


def test_矩阵缺失时为空():
    assert parseApprovalMatrix({}) == ()
    assert parseApprovalMatrix({"approvalMatrix": []}) == ()


def test_按三段键选中矩阵条目():
    entries = parseApprovalMatrix({"approvalMatrix": _VALID_MATRIX})

    picked = selectMatrixEntries(
        entries,
        subjectType="workflow_instance",
        capabilityCode="purchase_order_approve",
        riskLevel="MEDIUM",
    )
    assert len(picked) == 1
    assert picked[0].matrixCode == "po_medium_manager"


def test_风险级别不匹配则选不中():
    entries = parseApprovalMatrix({"approvalMatrix": _VALID_MATRIX})

    picked = selectMatrixEntries(
        entries,
        subjectType="workflow_instance",
        capabilityCode="purchase_order_approve",
        riskLevel="HIGH",
    )
    assert picked == ()


def test_解析定义风险级别():
    assert parseDefinitionRiskLevel({"riskLevel": "MEDIUM"}) == "MEDIUM"
    assert parseDefinitionRiskLevel({}) is None
    assert parseDefinitionRiskLevel(None) is None


def test_定义风险级别非法则拒绝():
    with pytest.raises(ValidationError):
        parseDefinitionRiskLevel({"riskLevel": "   "})


def test_解析步骤():
    steps = parseWorkflowSteps(
        {
            "steps": [
                {"code": "collect", "order": 0, "commandType": "po.collect"},
                {"code": "approve", "order": 1, "requiresApproval": True},
            ]
        }
    )

    assert [s.code for s in steps] == ["collect", "approve"]
    assert steps[0].requiresApproval is False
    assert steps[1].requiresApproval is True
    # 步骤↔命令的链接（编排器据此入队）
    assert steps[0].commandType == "po.collect"
    assert steps[1].commandType is None


def test_步骤缺编码则拒绝():
    with pytest.raises(ValidationError):
        parseWorkflowSteps({"steps": [{"order": 0, "commandType": "po.collect"}]})


def test_定义缺步骤则拒绝():
    """缺 ``steps`` ⇒ **失败关闭**（不是「无步骤 = 一步即完成」）。

    早期实现对缺失的 ``steps`` 返回空元组 ⇒ 定义漏写步骤时，提交会落一个
    **零步骤**的实例 ⇒ 编排器走「没有步骤 ⇒ 直接 COMPLETED」⇒ **外部写一次
    都没发生**，接口却报成功 ⇒ 假成功（G6 第一条「写操作全部经 Command」在
    这条路径上被绕过且无人察觉）。
    """

    with pytest.raises(ValidationError):
        parseWorkflowSteps({"riskLevel": "MEDIUM"})


def test_步骤为空数组则拒绝():
    """``steps: []`` 与「缺 steps」同义（零步骤 = 零动作）⇒ 一律拒绝。"""

    with pytest.raises(ValidationError):
        parseWorkflowSteps({"steps": []})


def test_定义体为空则拒绝():
    """``definition_json`` 为空 ⇒ 同样没有步骤 ⇒ 拒绝。"""

    with pytest.raises(ValidationError):
        parseWorkflowSteps(None)


def test_步骤编码重复则拒绝():
    with pytest.raises(ValidationError):
        parseWorkflowSteps(
            {
                "steps": [
                    {"code": "a", "commandType": "po.a"},
                    {"code": "a", "commandType": "po.b"},
                ]
            }
        )


# =============================================================================
# 审批运行时（真实 PG）
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


async def _makeInstance(dbSession, definition, *, createdBy="submitter"):
    from app.services.workflow_store import WorkflowStore

    store = WorkflowStore()
    instance, _ = await store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="inst-1",
        requestJson={"amount": 100},
        actor=createdBy,
    )
    await dbSession.commit()
    # 审批只能在运行中的实例上发起（PENDING → RUNNING）
    await store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    return instance


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


# =============================================================================
# 生成
# =============================================================================


async def test_生成审批任务(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)

    tasks = await _requestApproval(dbSession, instance)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "PENDING"
    assert task.matrix_code == "po_medium_manager"
    assert task.approver_role == "purchase_manager"
    assert task.subject_type == "workflow_instance"
    assert task.subject_id == str(instance.id)
    assert task.due_at is not None
    # 待审批时没有签署人（谁有资格批由矩阵决定）
    assert task.approver_user is None
    # 本轮发起人被记录（四眼原则的第二个比对对象）
    assert task.created_by == "submitter"


async def test_生成审批任务幂等(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)

    first = await _requestApproval(dbSession, instance)
    second = await _requestApproval(dbSession, instance)

    assert [t.id for t in first] == [t.id for t in second]
    rows = (
        (await dbSession.execute(select(ApprovalTask).where(ApprovalTask.instance_id == instance.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_矩阵未覆盖则失败关闭(dbSession, definition):
    """定义的风险级别没有对应矩阵条目 ⇒ 拒绝发起审批（不放行）。"""

    instance = await _makeInstance(dbSession, definition)
    instanceId = instance.id

    definition.definition_json = {**definition.definition_json, "riskLevel": "HIGH"}
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _service.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.rollback()

    rows = (
        (await dbSession.execute(select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_定义未声明风险级别则失败关闭(dbSession, definition):
    """风险级别只由服务端绑定：定义没写 ⇒ 拒绝（客户端也补不了）。"""

    instance = await _makeInstance(dbSession, definition)
    instanceId = instance.id

    definition.definition_json = {
        k: v for k, v in definition.definition_json.items() if k != "riskLevel"
    }
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _service.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.rollback()


async def test_审批人角色不存在则拒绝发起(dbSession, approverRole):
    """审批人查无 ⇒ 拒绝（否则落一条谁也批不了的任务）。"""

    definition = WorkflowDefinition(
        domain_code="procurement",
        capability_code="purchase_order_approve",
        version="v2",
        definition_json={"riskLevel": "MEDIUM", "approvalMatrix": _VALID_MATRIX},
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(definition)
    await dbSession.commit()
    # 角色存在，但矩阵里用的是另一个不存在的角色
    definition.definition_json = {
        "riskLevel": "MEDIUM",
        "approvalMatrix": [{**_VALID_MATRIX[0], "approverRole": "ghost_role"}],
    }
    await dbSession.commit()

    instance = await _makeInstance(dbSession, definition)
    instanceId = instance.id

    with pytest.raises(ConflictError):
        await _service.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.rollback()

    rows = (
        (await dbSession.execute(select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_指定审批人必须存在且启用(dbSession, definition):
    """矩阵指定人（approverUser）查无 / 已禁用 ⇒ 拒绝发起。"""

    instance = await _makeInstance(dbSession, definition)
    instanceId = instance.id

    definition.definition_json = {
        "riskLevel": "MEDIUM",
        "approvalMatrix": [{**_VALID_MATRIX[0], "approverRole": None, "approverUser": "ghost"}],
    }
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _service.requestApproval(dbSession, instanceId=instanceId, actor="submitter")
    await dbSession.rollback()


async def test_指定审批人存在则可发起并可决策(dbSession, definition):
    """矩阵指定人（存在且启用）⇒ 只有他能批。"""

    dbSession.add(User(username="alice", display_name="Alice", enabled=True))
    await dbSession.commit()

    definition.definition_json = {
        "riskLevel": "MEDIUM",
        "approvalMatrix": [{**_VALID_MATRIX[0], "approverRole": None, "approverUser": "alice"}],
    }
    await dbSession.commit()

    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)
    assert len(tasks) == 1
    # ⚠️ rollback 会 expire ORM 实例 ⇒ 先取 id
    taskId = tasks[0].id

    # 角色对但不是指定人 ⇒ 拒
    with pytest.raises(PermissionDeniedError):
        await _service.decideApproval(
            dbSession,
            taskId=taskId,
            decision="APPROVE",
            actor="manager",
            roles=("purchase_manager",),
        )
    await dbSession.rollback()

    # 指定人本人 ⇒ 可批
    await _service.decideApproval(
        dbSession,
        taskId=taskId,
        decision="APPROVE",
        actor="alice",
        roles=(),
    )
    await dbSession.commit()


async def test_已决过不允许叠加第二轮(dbSession, definition):
    """一个实例只允许一轮审批：第一轮决过后再发起 ⇒ 409。"""

    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    await _service.decideApproval(
        dbSession,
        taskId=tasks[0].id,
        decision="APPROVE",
        actor="manager",
        roles=("purchase_manager",),
    )
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _service.requestApproval(dbSession, instanceId=instance.id, actor="submitter")
    await dbSession.rollback()


# =============================================================================
# 对象级授权
# =============================================================================


async def test_无权者看不到实例(dbSession, definition):
    """无关第三方 ⇒ 404（不泄露「存在但无权」）。"""

    instance = await _makeInstance(dbSession, definition)
    instanceId = instance.id

    with pytest.raises(NotFoundError):
        await _service.assertInstanceVisible(
            dbSession,
            instanceId,
            actor="stranger",
            roles=("intern",),
        )


async def test_审批角色持有者可看实例(dbSession, definition):
    """审批持有人（**已校验身份**）可看实例。

    ⚠️ ``isRealIdentity=True``：跨用户可见性要求已校验身份（stub 身份的缺省
    角色带 admin ⇒ 只看角色会让任意客户端读穿工作流面）。本用例断的是
    **授权维度**（审批持有人可读），不是认证维度 ⇒ 显式给真实身份。
    """

    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)
    taskId = tasks[0].id

    visible = await _service.assertInstanceVisible(
        dbSession,
        instance.id,
        actor="manager",
        roles=("purchase_manager",),
        isRealIdentity=True,
    )
    assert visible.id == instance.id
    assert taskId is not None


async def test_发起审批需对实例可见(dbSession, definition):
    """无权者不能替别人发起审批（404）。"""

    instance = await _makeInstance(dbSession, definition)
    instanceId = instance.id

    with pytest.raises(NotFoundError):
        await _service.requestApproval(
            dbSession,
            instanceId=instanceId,
            actor="stranger",
            roles=("intern",),
        )
    await dbSession.rollback()


# =============================================================================
# 决策守卫
# =============================================================================


async def test_非授权角色不能审批(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    with pytest.raises(PermissionDeniedError):
        await _service.decideApproval(
            dbSession,
            taskId=tasks[0].id,
            decision="APPROVE",
            actor="someone_else",
            roles=("intern",),
        )
    await dbSession.rollback()


async def test_提交人不能自审批(dbSession, definition):
    """四眼原则：提交人即使有审批角色也不能批自己的单。"""

    instance = await _makeInstance(dbSession, definition, createdBy="boss")
    tasks = await _requestApproval(dbSession, instance, actor="boss")

    with pytest.raises(PermissionDeniedError):
        await _service.decideApproval(
            dbSession,
            taskId=tasks[0].id,
            decision="APPROVE",
            actor="boss",
            roles=("purchase_manager",),
        )
    await dbSession.rollback()


async def test_本轮发起人不能审批(dbSession, definition):
    """四眼原则第二个比对对象：由他人代发起的审批，发起人自己也不能批。"""

    instance = await _makeInstance(dbSession, definition, createdBy="submitter")
    # admin 代为发起（admin 对实例可见 ⇒ 已校验身份的跨用户 admin）
    tasks = await _requestApproval(
        dbSession,
        instance,
        actor="boss",
        roles=("admin",),
        isRealIdentity=True,
    )

    with pytest.raises(PermissionDeniedError):
        await _service.decideApproval(
            dbSession,
            taskId=tasks[0].id,
            decision="APPROVE",
            actor="boss",
            roles=("purchase_manager", "admin"),
        )
    await dbSession.rollback()


async def test_重复审批被拒(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    await _service.decideApproval(
        dbSession,
        taskId=tasks[0].id,
        decision="APPROVE",
        actor="manager",
        roles=("purchase_manager",),
    )
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _service.decideApproval(
            dbSession,
            taskId=tasks[0].id,
            decision="APPROVE",
            actor="manager",
            roles=("purchase_manager",),
        )
    await dbSession.rollback()


async def test_超时后不能审批且任务转TIMEOUT(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    # 手工把截止时间推到过去 ⇒ 模拟超时
    task = tasks[0]
    task.due_at = task.due_at.replace(year=task.due_at.year - 1)
    await dbSession.commit()
    taskId = task.id
    instanceId = instance.id

    with pytest.raises(ConflictError):
        await _service.decideApproval(
            dbSession,
            taskId=taskId,
            decision="APPROVE",
            actor="manager",
            roles=("purchase_manager",),
        )

    row = (
        await dbSession.execute(select(ApprovalTask).where(ApprovalTask.id == taskId))
    ).scalar_one()
    assert row.status == "TIMEOUT"

    from app.domain.workflow_models import WorkflowInstance

    instanceRow = (
        await dbSession.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
    ).scalar_one()
    assert instanceRow.status == "FAILED"


async def test_全部通过则实例变APPROVED(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    await _service.decideApproval(
        dbSession,
        taskId=tasks[0].id,
        decision="APPROVE",
        actor="manager",
        roles=("purchase_manager",),
    )
    await dbSession.commit()

    current = await _service.getInstance(dbSession, instance.id)
    assert current is not None
    assert current.status == "APPROVED"


async def test_任一拒绝则实例变REJECTED(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    await _service.decideApproval(
        dbSession,
        taskId=tasks[0].id,
        decision="REJECT",
        note="预算不足",
        actor="manager",
        roles=("purchase_manager",),
    )
    await dbSession.commit()

    current = await _service.getInstance(dbSession, instance.id)
    assert current is not None
    assert current.status == "REJECTED"


async def test_超时清扫把待审批标记为TIMEOUT(dbSession, definition):
    instance = await _makeInstance(dbSession, definition)
    tasks = await _requestApproval(dbSession, instance)

    task = tasks[0]
    task.due_at = task.due_at.replace(year=task.due_at.year - 1)
    await dbSession.commit()

    swept = await _service.sweepTimedOutApprovals(dbSession)
    await dbSession.commit()
    assert swept == 1

    rows = (
        (await dbSession.execute(select(ApprovalTask).where(ApprovalTask.instance_id == instance.id)))
        .scalars()
        .all()
    )
    assert rows[0].status == "TIMEOUT"
