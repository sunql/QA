"""P5 编排器：把「当前步骤」翻译成业务命令（真实 PostgreSQL）。

W5 评审发现的 CRITICAL 缺口
---------------------------
``store.enqueueCommand`` 在**生产路径上没有任何调用方**（只被测试调用）⇒
Worker 在生产永远领不到活儿 ⇒ G6 第一条「写操作全部经 Command」名存实亡
（步骤与命令之间没有链接，也没有驱动者）。

根因有两处：

1. ``WorkflowStepSpec`` 只有 code / order / requiresApproval ⇒ **步骤↔命令无链接**；
2. 没有任何「编排器」把步骤推进成命令 ⇒ 提交后实例永远停在 RUNNING。

本文件钉住编排器的纪律：

1. **命令必须有来路**：提交实例 ⇒ 编排器为当前步骤入队命令（否则 Worker 空转）。
2. **审批先行**：步骤要求审批 ⇒ 先发起审批轮次、**不入队命令**；通过后才入队。
3. **定义即契约**：步骤要么有 ``commandType``，要么 ``requiresApproval=true``；
   两者皆无 ⇒ 解析期拒绝（失败关闭），绝不「悄悄跳过」。
4. **单轮审批**：一个定义最多一个审批环节（与审批面「单轮」守卫一致）。
5. **幂等**：重复驱动不重复入队（命令幂等键 = 实例 + 步骤）。
6. **快照**：编排快照写进 ``workflow_step.input_json`` ⇒ 定义事后被改
   也不影响在跑的实例。
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
from app.domain.exceptions import ValidationError
from app.models.rbac import Role
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import CommandHandlerRegistry, SimulatedCommandHandler
from app.services.workflow_definition import parseWorkflowSteps
from app.services.workflow_orchestrator import WorkflowOrchestrator, commandIdempotencyKey
from app.services.workflow_service import WorkflowService
from app.services.workflow_store import WorkflowStore

_service = WorkflowService()
_store = WorkflowStore()
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


# =============================================================================
# 夹具 / 助手
# =============================================================================


@pytest.fixture()
async def approverRole(dbSession: AsyncSession) -> Role:
    """矩阵里的审批角色必须真实存在（否则生成谁也批不了的任务）。"""

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
    idempotencyKey: str = "orch-1",
    actor: str = "submitter",
    request: dict | None = None,
) -> tuple[WorkflowInstance, bool]:
    instance, created = await _service.submitInstance(
        dbSession,
        domainCode=definition.domain_code,
        capabilityCode=definition.capability_code,
        version=definition.version,
        idempotencyKey=idempotencyKey,
        requestJson=request if request is not None else {"amount": 100},
        actor=actor,
    )
    await dbSession.commit()
    return instance, created


async def _steps(dbSession: AsyncSession, instanceId: int) -> list[WorkflowStep]:
    rows = (
        (
            await dbSession.execute(
                select(WorkflowStep)
                .where(WorkflowStep.instance_id == instanceId)
                .order_by(WorkflowStep.step_order, WorkflowStep.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _commands(dbSession: AsyncSession, instanceId: int) -> list[BusinessCommand]:
    rows = (
        (
            await dbSession.execute(
                select(BusinessCommand)
                .where(BusinessCommand.instance_id == instanceId)
                .order_by(BusinessCommand.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _approvals(dbSession: AsyncSession, instanceId: int) -> list[ApprovalTask]:
    rows = (
        (
            await dbSession.execute(
                select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _executor() -> CommandExecutor:
    registry = CommandHandlerRegistry()
    registry.register("po.collect", SimulatedCommandHandler())
    registry.register("po.write", SimulatedCommandHandler())
    return CommandExecutor(registry, maxAttempts=3)


# =============================================================================
# 定义契约（纯函数）
# =============================================================================


def test_步骤既无命令也不需审批则拒绝():
    """没有可执行动作的步骤 = 定义写错了 ⇒ 失败关闭（不悄悄跳过）。"""

    with pytest.raises(ValidationError):
        parseWorkflowSteps({"steps": [{"code": "collect", "order": 0}]})


def test_有命令的步骤合法():
    specs = parseWorkflowSteps({"steps": [{"code": "write", "order": 0, "commandType": "po.write"}]})

    assert len(specs) == 1
    assert specs[0].commandType == "po.write"
    assert specs[0].requiresApproval is False


def test_纯审批门步骤合法():
    specs = parseWorkflowSteps({"steps": [{"code": "gate", "order": 0, "requiresApproval": True}]})

    assert specs[0].commandType is None
    assert specs[0].requiresApproval is True


def test_多个审批环节被拒():
    """审批面只支持单轮（守卫⑥）⇒ 定义里出现第二个审批环节就是不可执行。"""

    with pytest.raises(ValidationError):
        parseWorkflowSteps(
            {
                "steps": [
                    {"code": "a", "order": 0, "requiresApproval": True},
                    {"code": "b", "order": 1, "requiresApproval": True},
                ]
            }
        )


def test_步骤载荷必须是非空对象():
    with pytest.raises(ValidationError):
        parseWorkflowSteps({"steps": [{"code": "x", "order": 0, "payload": []}]})


# =============================================================================
# 提交即入队（命令的来路）
# =============================================================================


async def test_提交实例后自动入队首条命令(dbSession: AsyncSession):
    """提交后编排器把「当前步骤」翻译成命令 ⇒ Worker 才有活儿可领。"""

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )

    instance, created = await _submit(dbSession, definition)

    assert created is True
    commands = await _commands(dbSession, instance.id)
    assert len(commands) == 1
    assert commands[0].command_type == "po.write"
    assert commands[0].status == "PENDING"
    steps = await _steps(dbSession, instance.id)
    assert steps[0].status == "RUNNING"
    assert instance.status == "RUNNING"


async def test_命令载荷按命名空间拆分不摊平(dbSession: AsyncSession):
    """命令载荷**命名空间化**：``{"request": …, "step": …}``。

    为什么（旧实现是「摊平合并」）：``{**request_json, **step_payload}`` ⇒
    **用户输入与工作流作者的步骤字段混在一个字典里** ⇒ 适配器分不清哪个字段
    是流程规定的、哪个是用户夹带的 ⇒ 后续新增字段时，用户输入会**默默顶掉**
    流程字段（典型的 mass-assignment）。
    """

    definition = await _seedDefinition(
        dbSession,
        [{"code": "write", "order": 0, "commandType": "po.write", "payload": {"extra": 1}}],
    )

    instance, _ = await _submit(dbSession, definition, request={"amount": 100})

    commands = await _commands(dbSession, instance.id)
    assert commands[0].payload == {"request": {"amount": 100}, "step": {"extra": 1}}


async def test_用户请求不能覆盖步骤载荷(dbSession: AsyncSession):
    """用户输入**顶不掉**流程字段（命名空间隔离 ⇒ 各自独立）。"""

    definition = await _seedDefinition(
        dbSession,
        [{"code": "write", "order": 0, "commandType": "po.write", "payload": {"extra": 1}}],
    )

    instance, _ = await _submit(dbSession, definition, request={"extra": 999})

    commands = await _commands(dbSession, instance.id)
    payload = commands[0].payload
    assert payload["step"]["extra"] == 1
    assert payload["request"]["extra"] == 999


async def test_用户请求不能注入适配器控制键(dbSession: AsyncSession):
    """``simulate`` 是**适配器控制面**（演练指令）⇒ 不得来自用户输入。

    回归点：旧实现摊平合并 ⇒ 用户在请求里塞 ``simulate`` ⇒ 演练指令直达
    适配器（能强制让命令失败 / 改写行为）⇒ 用户能左右治理面流程。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )

    instance, _ = await _submit(
        dbSession, definition, request={"amount": 100, "simulate": {"outcome": "fail"}}
    )

    commands = await _commands(dbSession, instance.id)
    payload = commands[0].payload
    assert "simulate" not in payload
    assert "simulate" not in payload["request"]
    assert payload["request"]["amount"] == 100


async def test_步骤行携带编排快照(dbSession: AsyncSession):
    """快照落在步骤行上 ⇒ 定义事后被改也不影响在跑的实例。"""

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )

    instance, _ = await _submit(dbSession, definition)

    steps = await _steps(dbSession, instance.id)
    assert steps[0].input_json["commandType"] == "po.write"
    assert steps[0].input_json["requiresApproval"] is False


async def test_重复驱动不重复入队(dbSession: AsyncSession):
    """幂等：命令幂等键 = 实例 + 步骤 ⇒ 重入驱动**不新建**命令。

    ⚠️ **这条用例早先是假绿**：第二次 ``drive`` 时步骤已是 RUNNING ⇒ 编排器在
    「当前步骤还在跑 ⇒ 等 Worker」处就返回了 ⇒ **压根没再调用**
    ``enqueueCommand`` ⇒ 断言「只有一条命令」证明的是「步骤没被重复推进」，
    而**不是**「幂等键生效」⇒ 把 ``enqueueCommand`` 的幂等性删掉，这用例照样绿。

    ⇒ 现在摆出**真实的重入场景**：上一次驱动已经入队了命令，但在推进步骤前
    崩了（步骤仍是 PENDING）⇒ 重入驱动会**再次走到入队口** ⇒ 必须不新建命令。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    # 直接造实例 + 步骤（不走 submit 的首次驱动，避免把步骤推成 RUNNING）
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="orch-reentrant",
        requestJson={"amount": 100},
        actor="submitter",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[
            {
                "code": "write",
                "order": 0,
                "input": {"commandType": "po.write", "requiresApproval": False, "payload": {}},
            }
        ],
    )
    await dbSession.commit()

    # 模拟「上次驱动入队了命令，但还没推进步骤就崩了」
    await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=steps[0].id,
        commandType="po.write",
        idempotencyKey=commandIdempotencyKey(instance.id, steps[0].id),
        payload={},
    )
    await dbSession.commit()
    assert len(await _commands(dbSession, instance.id)) == 1

    # 重入驱动 ⇒ 仍只有一条命令（幂等键生效）
    await _orchestrator.drive(dbSession, instanceId=instance.id)
    await dbSession.commit()

    commands = await _commands(dbSession, instance.id)
    assert len(commands) == 1
    assert commands[0].status == "PENDING"


# =============================================================================
# 多步推进
# =============================================================================


async def test_命令成功后推进下一步(dbSession: AsyncSession):
    definition = await _seedDefinition(
        dbSession,
        [
            {"code": "collect", "order": 0, "commandType": "po.collect"},
            {"code": "write", "order": 1, "commandType": "po.write"},
        ],
    )
    instance, _ = await _submit(dbSession, definition)
    commands = await _commands(dbSession, instance.id)
    assert len(commands) == 1
    executor = _executor()

    await executor.execute(dbSession, commands[0].id, workerId="worker-1")

    steps = await _steps(dbSession, instance.id)
    assert steps[0].status == "SUCCEEDED"
    assert steps[1].status == "RUNNING"
    commands = await _commands(dbSession, instance.id)
    assert [c.command_type for c in commands] == ["po.collect", "po.write"]
    assert instance.status == "RUNNING"


async def test_全部步骤完成后实例完成(dbSession: AsyncSession):
    definition = await _seedDefinition(
        dbSession,
        [
            {"code": "collect", "order": 0, "commandType": "po.collect"},
            {"code": "write", "order": 1, "commandType": "po.write"},
        ],
    )
    instance, _ = await _submit(dbSession, definition)
    executor = _executor()

    first = (await _commands(dbSession, instance.id))[0]
    await executor.execute(dbSession, first.id, workerId="worker-1")
    second = (await _commands(dbSession, instance.id))[1]
    await executor.execute(dbSession, second.id, workerId="worker-1")

    steps = await _steps(dbSession, instance.id)
    assert [s.status for s in steps] == ["SUCCEEDED", "SUCCEEDED"]
    assert instance.status == "COMPLETED"


async def test_零步骤实例直接收尾(dbSession: AsyncSession):
    """**历史数据**里若已有零步骤实例 ⇒ 编排器直接收尾（不吊在 RUNNING）。

    ⚠️ 这条路径现在**提交面已不可达**：定义必须声明至少一步
    （``parseWorkflowSteps`` 失败关闭 ⇒ 提交就 422）⇒ 零步骤实例只可能来自
    旧数据 / 手工改库。⇒ 编排器仍要能把它收干净（不吊死）。
    """

    definition = await _seedDefinition(
        dbSession, [{"code": "write", "order": 0, "commandType": "po.write"}]
    )
    # 绕过提交面，直接造一个「有定义、无步骤行」的实例（模拟旧数据）。
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="legacy-no-steps",
        requestJson={},
        actor="submitter",
    )
    await dbSession.commit()

    driven = await _orchestrator.drive(dbSession, instanceId=instance.id)
    await dbSession.commit()

    assert driven.status == "COMPLETED"


# =============================================================================
# 审批先行
# =============================================================================


async def test_需要审批的步骤先发起审批且不入队命令(dbSession: AsyncSession, approverRole):
    definition = await _seedDefinition(
        dbSession,
        [{"code": "approve", "order": 0, "requiresApproval": True, "commandType": "po.write"}],
        matrix=_MATRIX,
    )

    instance, _ = await _submit(dbSession, definition)

    approvals = await _approvals(dbSession, instance.id)
    assert len(approvals) == 1
    assert approvals[0].status == "PENDING"
    assert await _commands(dbSession, instance.id) == []
    assert instance.status == "WAITING_APPROVAL"


async def test_审批通过后入队命令(dbSession: AsyncSession, approverRole):
    definition = await _seedDefinition(
        dbSession,
        [{"code": "approve", "order": 0, "requiresApproval": True, "commandType": "po.write"}],
        matrix=_MATRIX,
    )
    instance, _ = await _submit(dbSession, definition)
    approvals = await _approvals(dbSession, instance.id)

    await _approval.decideApproval(
        dbSession,
        taskId=approvals[0].id,
        decision="APPROVE",
        actor="manager",
        roles=[_APPROVER_ROLE],
    )
    await dbSession.commit()
    await _orchestrator.drive(dbSession, instanceId=instance.id)
    await dbSession.commit()

    commands = await _commands(dbSession, instance.id)
    assert len(commands) == 1
    assert commands[0].status == "PENDING"
    steps = await _steps(dbSession, instance.id)
    assert steps[0].status == "RUNNING"
    assert instance.status == "RUNNING"


async def test_审批拒绝后不入队命令(dbSession: AsyncSession, approverRole):
    definition = await _seedDefinition(
        dbSession,
        [{"code": "approve", "order": 0, "requiresApproval": True, "commandType": "po.write"}],
        matrix=_MATRIX,
    )
    instance, _ = await _submit(dbSession, definition)
    approvals = await _approvals(dbSession, instance.id)

    await _approval.decideApproval(
        dbSession,
        taskId=approvals[0].id,
        decision="REJECT",
        actor="manager",
        roles=[_APPROVER_ROLE],
    )
    await dbSession.commit()
    await _orchestrator.drive(dbSession, instanceId=instance.id)
    await dbSession.commit()

    assert await _commands(dbSession, instance.id) == []
    assert instance.status == "REJECTED"


async def test_纯审批门通过后步骤完成(dbSession: AsyncSession, approverRole):
    """没有命令的审批门：审批通过即该步骤完成 ⇒ 实例可以收尾。"""

    definition = await _seedDefinition(
        dbSession,
        [{"code": "gate", "order": 0, "requiresApproval": True}],
        matrix=_MATRIX,
    )
    instance, _ = await _submit(dbSession, definition)
    approvals = await _approvals(dbSession, instance.id)

    await _approval.decideApproval(
        dbSession,
        taskId=approvals[0].id,
        decision="APPROVE",
        actor="manager",
        roles=[_APPROVER_ROLE],
    )
    await dbSession.commit()
    await _orchestrator.drive(dbSession, instanceId=instance.id)
    await dbSession.commit()

    steps = await _steps(dbSession, instance.id)
    assert steps[0].status == "SUCCEEDED"
    assert instance.status == "COMPLETED"


async def test_审批未决时重复驱动不会叠加审批轮次(dbSession: AsyncSession, approverRole):
    definition = await _seedDefinition(
        dbSession,
        [{"code": "approve", "order": 0, "requiresApproval": True, "commandType": "po.write"}],
        matrix=_MATRIX,
    )
    instance, _ = await _submit(dbSession, definition)

    await _orchestrator.drive(dbSession, instanceId=instance.id)
    await dbSession.commit()

    approvals = await _approvals(dbSession, instance.id)
    assert len(approvals) == 1
