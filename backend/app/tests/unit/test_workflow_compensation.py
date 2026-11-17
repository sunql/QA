"""P5 **补偿收敛**单测（补偿路径的步骤终态 + 命令终态）。

为什么单独成文件：``test_workflow_command_executor.py`` 已逼近仓库 800 行硬上限
（单文件 200–400 行为宜，不超过 800 行）⇒ 补偿相关的用例集中到这里，
执行器文件只留「执行 / 重试 / 失败隔离」。

被测纪律：

1. **实例进终态 ⇒ 没有悬空步骤**：实例 COMPENSATED 时，
   **不得**还有步骤停在 ``PENDING`` / ``RUNNING``（步骤矩阵里这两个状态都有出边
   ⇒ 停在它们上面 = 这一轮还没结束，但实例已经结账）。
2. **两条语义不可混**：命令跑过并被回滚 ⇒ 步骤 ``COMPENSATED``；
   命令**没跑成功**（从没入队 / 终态失败 / 被取消）⇒ 步骤 ``SKIPPED``。
3. **补偿失败隔离**：单条补偿抛错不得回滚已完成的补偿（SAVEPOINT 语义）。

全程走**真实 PostgreSQL**（``workflow_*`` 表是真表、真事务）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.models import AuditLog
from app.domain.workflow_models import (
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import (
    CommandHandlerRegistry,
    SimulatedCommandHandler,
)
from app.services.workflow_store import WorkflowStore

_store = WorkflowStore()


@pytest.fixture()
async def definition(dbSession) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="po_write",
        version="v1",
        definition_json={"steps": [{"code": "write", "order": 0}]},
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _instanceWithSteps(
    dbSession,
    definition,
    *,
    idempotencyKey: str,
    codes: list[str],
    runningCount: int,
):
    """建实例 + 若干步骤；前 ``runningCount`` 个步骤 RUNNING，其余 PENDING。

    ⚠️ 必须**多步骤**：单步骤实例在第一条命令成功后会被编排器收成 COMPLETED
    ⇒ 归属实例不再「活着」⇒ 后续命令按契约不得再被领取（补偿就无从谈起）。
    """

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey=idempotencyKey,
        requestJson={},
        actor="submitter",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": code, "order": index} for index, code in enumerate(codes)],
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    for step in steps[:runningCount]:
        await _store.advanceStep(dbSession, step.id, "RUNNING")
    await dbSession.commit()
    return instance, steps


async def _enqueue(
    dbSession,
    instance,
    step,
    *,
    commandType: str = "po.write",
    key: str,
    payload: dict | None = None,
):
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=step.id,
        commandType=commandType,
        idempotencyKey=key,
        payload=payload or {},
    )
    await dbSession.commit()
    return command


def _executor(handler=None, *, maxAttempts: int = 3) -> CommandExecutor:
    registry = CommandHandlerRegistry()
    registry.register("po.write", handler or SimulatedCommandHandler())
    return CommandExecutor(registry, maxAttempts=maxAttempts)


async def _instanceWithStep(dbSession, definition, *, idempotencyKey: str = "comp-step-1"):
    """单步骤实例（步骤 RUNNING ⇒ 可被补偿）。"""

    instance, steps = await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey=idempotencyKey,
        codes=["write"],
        runningCount=1,
    )
    return instance, steps[0]


async def _instanceWithRunningSteps(dbSession, definition, *, idempotencyKey: str, count: int = 2):
    """``count`` 个步骤且**全部 RUNNING**（多步骤 ⇒ 实例不会被编排器提前收成 COMPLETED）。"""

    return await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey=idempotencyKey,
        codes=[f"write-{index}" for index in range(count)],
        runningCount=count,
    )


async def _instanceStatus(dbSession, instanceId: int) -> str:
    """读实例状态（``populate_existing`` ⇒ 不吃 identity map 里的旧值）。"""

    row = (
        await dbSession.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.id == instanceId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.status


async def _stepRow(dbSession, stepId: int) -> WorkflowStep:
    """读步骤行（``populate_existing`` ⇒ 不吃 identity map 里的旧值）。"""

    return (
        await dbSession.execute(
            select(WorkflowStep)
            .where(WorkflowStep.id == stepId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# =============================================================================
# R12-A：补偿 ⇒ 没有步骤吊在 PENDING / RUNNING
# =============================================================================


async def test_补偿后未开始的步骤记SKIPPED(dbSession, definition):
    """**从没入队**的步骤（命令还没生成）⇒ 补偿时必须记 SKIPPED。

    回归点：补偿只处理「**被取消的命令**所挂的步骤」⇒ 从没入队的步骤
    （``PENDING``）根本不在那个集合里 ⇒ 步骤吊在 PENDING，而实例已 COMPENSATED
    ⇒ 状态自相矛盾（步骤矩阵里 PENDING 有出边 = 这一轮还没结束）。
    """

    instance, steps = await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey="comp-pending-step",
        codes=["write", "later"],
        runningCount=1,
    )
    okCommand = await _enqueue(dbSession, instance, steps[0], key="comp-pending-ok")
    executor = _executor()

    instanceId = instance.id
    okId = okCommand.id
    laterStepId = steps[1].id

    await executor.execute(dbSession, okId, workerId="worker-1")
    await executor.compensateInstance(dbSession, instanceId, reason="人工补偿")

    stepRow = await _stepRow(dbSession, laterStepId)
    assert stepRow.status == "SKIPPED"


async def test_补偿后终态失败命令的步骤记SKIPPED(dbSession, definition):
    """**终态失败**（ABORTED）的那条命令所挂的步骤 ⇒ 记 SKIPPED（不是吊在 RUNNING）。

    回归点：``ABORTED`` 不在「可取消命令」集合里 ⇒ 该步骤既不在
    ``cancelledStepIds``（⇒ 不会 SKIPPED）也不在 ``compensatedStepIds``
    （⇒ 不会 COMPENSATED）⇒ 步骤永远停在 RUNNING，而实例已 COMPENSATED。
    """

    instance, steps = await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey="comp-aborted-step",
        codes=["write"],
        runningCount=1,
    )
    failCommand = await _enqueue(
        dbSession,
        instance,
        steps[0],
        key="comp-aborted-bad",
        payload={"simulate": {"outcome": "fail", "message": "下游超时"}},
    )
    executor = _executor(maxAttempts=1)

    instanceId = instance.id
    failId = failCommand.id
    stepId = steps[0].id

    outcome = await executor.execute(dbSession, failId, workerId="worker-1")
    assert outcome.status == "ABORTED"

    instanceRow = (
        await dbSession.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.id == instanceId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert instanceRow.status == "COMPENSATED"

    stepRow = await _stepRow(dbSession, stepId)
    assert stepRow.status == "SKIPPED"


async def test_补偿后所有步骤都收敛(dbSession, definition):
    """**不变量**（一眼可验）：实例 COMPENSATED ⇒ 没有步骤停在 PENDING/RUNNING。"""

    instance, steps = await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey="comp-invariant",
        codes=["write", "later", "third"],
        runningCount=2,
    )
    okCommand = await _enqueue(dbSession, instance, steps[0], key="comp-inv-ok")
    # 这条命令只入队不执行 ⇒ 补偿时它会被取消（⇒ 对应步骤 SKIPPED）。
    await _enqueue(dbSession, instance, steps[1], key="comp-inv-pending")
    executor = _executor()

    instanceId = instance.id
    okId = okCommand.id

    await executor.execute(dbSession, okId, workerId="worker-1")
    await executor.compensateInstance(dbSession, instanceId, reason="人工补偿")

    stepRows = (
        (
            await dbSession.execute(
                select(WorkflowStep)
                .where(WorkflowStep.instance_id == instanceId)
                .order_by(WorkflowStep.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    statuses = [row.status for row in stepRows]
    assert statuses == ["COMPENSATED", "SKIPPED", "SKIPPED"]
    assert not [status for status in statuses if status in {"PENDING", "RUNNING"}]


async def test_补偿后待执行命令不再被领取(dbSession, definition):
    """补偿 ⇒ 同实例的**待执行命令**一并取消（否则「回滚」只是纸面动作）。"""

    instance, steps = await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey="comp-cancel-cmd",
        codes=["write", "later"],
        runningCount=2,
    )
    bad = await _enqueue(
        dbSession,
        instance,
        steps[0],
        key="comp-cancel-bad",
        payload={"simulate": {"outcome": "fail"}},
    )
    good = await _enqueue(dbSession, instance, steps[1], key="comp-cancel-good")
    executor = _executor(maxAttempts=1)

    badId = bad.id
    goodId = good.id

    await executor.execute(dbSession, badId, workerId="worker-1")

    goodRow = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == goodId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert goodRow.status == "CANCELLED"

    # 取消后的命令**领不走**（Worker 不会再写外部系统）
    claimed = await _store.claimNextCommand(dbSession, workerId="worker-2", maxAttempts=1)
    assert claimed is None


# =============================================================================
# 从执行器测试文件迁入的补偿用例（W3 完成定义）
# =============================================================================


async def test_补偿后实例的命令不再被领取(dbSession, definition):
    """补偿 ⇒ 实例终态 ⇒ 它的命令**不得**再被 Worker 领走（否则「补偿」只是纸面动作）。

    回归点：补偿前命令是 ``FAILED``（可重试）⇒ 旧实现只取消 ``PENDING``
    ⇒ ``FAILED`` 仍可被领取 ⇒ 已补偿的实例照样写外部系统。
    """

    instance, step = await _instanceWithStep(
        dbSession, definition, idempotencyKey="comp-claimed"
    )
    # ⚠️ 失败路径里的 ``session.rollback()`` 会 **expire 全部 ORM 实例**
    #    ⇒ 之后访问 ``instance.id`` 会触发同步懒加载 ⇒ MissingGreenlet。
    #    ⇒ 先把标量取出来。
    instanceId = instance.id
    failed = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-31",
        payload={"simulate": {"outcome": "fail"}},
    )
    executor = _executor(maxAttempts=3)

    await executor.execute(dbSession, failed.id, workerId="worker-1")
    await executor.compensateInstance(dbSession, instanceId, reason="人工补偿")

    claimed = await _store.claimNextCommand(dbSession, workerId="worker-2", maxAttempts=3)
    assert claimed is None


# =============================================================================
# 补偿
# =============================================================================

async def test_终态失败触发同实例成功命令的补偿(dbSession, definition):
    """W3 完成定义：注入失败后补偿执行。"""

    # ⚠️ 用**多步骤**实例：单步骤实例在第一条命令成功后即 COMPLETED ⇒ 归属实例
    #    不再「活着」⇒ 第二条命令按契约不得被领取（补偿也就无从谈起）。
    instance, steps = await _instanceWithRunningSteps(
        dbSession, definition, idempotencyKey="comp-trigger"
    )
    step = steps[0]
    okCommand = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-8")
    failCommand = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-9",
        payload={"simulate": {"outcome": "fail", "message": "写库失败"}},
    )
    handler = SimulatedCommandHandler()
    executor = _executor(handler, maxAttempts=1)

    await executor.execute(dbSession, okCommand.id, workerId="worker-1")
    outcome = await executor.execute(dbSession, failCommand.id, workerId="worker-1")

    assert outcome.status == "ABORTED"
    assert outcome.isTerminal is True

    compensated = (
        await dbSession.execute(
            select(BusinessCommand).where(BusinessCommand.id == okCommand.id)
        )
    ).scalar_one()
    assert compensated.status == "COMPENSATED"

    instanceRow = (
        await dbSession.execute(select(WorkflowInstance).where(WorkflowInstance.id == instance.id))
    ).scalar_one()
    assert instanceRow.status == "COMPENSATED"

async def test_补偿跳过不支持补偿的适配器(dbSession, definition):
    """适配器没有 compensate ⇒ 跳过并留痕，不假装补偿过。"""

    instance, step = await _instanceWithStep(dbSession, definition)

    class _NoCompensate:
        async def execute(self, session, *, command):
            return {"ok": True}

    okCommand = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-10")

    registry = CommandHandlerRegistry()
    registry.register("po.write", _NoCompensate())
    executor = CommandExecutor(registry, maxAttempts=1)

    await executor.execute(dbSession, okCommand.id, workerId="worker-1")
    outcomes = await executor.compensateInstance(dbSession, instance.id, reason="人工触发")

    assert [o.status for o in outcomes] == ["SKIPPED"]
    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == okCommand.id))
    ).scalar_one()
    assert row.status == "SUCCEEDED"

async def test_补偿中途失败不影响已完成的补偿(dbSession, definition):
    """一条补偿抛错**不得**回滚其它补偿（否则「补偿」名存实亡）。

    回归点：旧实现用整事务 ``rollback()`` 兜补偿异常 ⇒ 把**已经完成**的补偿
    （以及「取消待执行命令」）一起撤销 ⇒ 实例却仍被标成 COMPENSATED ⇒
    外部写照旧发生、审计却说「已补偿」。

    本用例钉住三件事：
    1. 失败点**之前**的补偿仍然落库；
    2. 失败点**之后**的补偿仍然执行（失败隔离）；
    3. 全程不抛 ``MissingGreenlet``（回滚后不再访问过期 ORM 实例的属性）。
    """

    # ⚠️ 多步骤实例：单步骤实例在第一条命令成功后即 COMPLETED ⇒ 后续命令
    #    按契约不得被领取（补偿就无从谈起）。
    instance, steps = await _instanceWithRunningSteps(
        dbSession, definition, idempotencyKey="comp-mixed"
    )
    step = steps[0]
    first = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-40")
    boom = await _enqueue(
        dbSession, instance, step, commandType="po.write", key="k-41", payload={"boom": True}
    )
    last = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-42")

    class _Mixed:
        async def execute(self, session, *, command):
            return {"ok": True}

        async def compensate(self, session, *, command):
            if (command.payload or {}).get("boom"):
                raise RuntimeError("补偿炸了")
            return {"compensated": True}

    registry = CommandHandlerRegistry()
    registry.register("po.write", _Mixed())
    executor = CommandExecutor(registry, maxAttempts=3)

    for command in (first, boom, last):
        await executor.execute(dbSession, command.id, workerId="worker-1")

    outcomes = await executor.compensateInstance(dbSession, instance.id, reason="人工触发")

    statuses = {o.commandId: o.status for o in outcomes}
    assert statuses[first.id] == "COMPENSATED"
    assert statuses[boom.id] == "FAILED"
    assert statuses[last.id] == "COMPENSATED"

    async def _statusOf(commandId: int) -> str:
        row = (
            await dbSession.execute(
                select(BusinessCommand).where(BusinessCommand.id == commandId)
            )
        ).scalar_one()
        return row.status

    assert await _statusOf(first.id) == "COMPENSATED"
    assert await _statusOf(boom.id) == "SUCCEEDED"
    assert await _statusOf(last.id) == "COMPENSATED"

async def test_补偿后步骤置COMPENSATED(dbSession, definition):
    instance, step = await _instanceWithStep(dbSession, definition)
    okCommand = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-12")
    executor = _executor()

    await executor.execute(dbSession, okCommand.id, workerId="worker-1")
    await executor.compensateInstance(dbSession, instance.id, reason="人工触发")

    stepRow = (
        await dbSession.execute(select(WorkflowStep).where(WorkflowStep.id == step.id))
    ).scalar_one()
    assert stepRow.status == "COMPENSATED"


# =============================================================================
# 失败隔离
# =============================================================================

async def test_补偿时被取消命令的步骤记SKIPPED(dbSession, definition):
    """补偿 ⇒ 待执行命令一并取消 ⇒ **对应步骤**必须记 SKIPPED。

    回归点：``cancelPendingCommands`` **返回**了被取消命令所挂的步骤 id，
    但执行器把它**丢弃**了 ⇒ 命令 CANCELLED、步骤还吊在 RUNNING ⇒
    实例已 COMPENSATED，步骤却永远停在「在跑」⇒ 状态自相矛盾。
    （步骤矩阵允许 ``PENDING → SKIPPED`` 与 ``RUNNING → SKIPPED``。）
    """

    instance, steps = await _instanceWithRunningSteps(
        dbSession, definition, idempotencyKey="comp-skip", count=2
    )
    okCommand = await _enqueue(dbSession, instance, steps[0], commandType="po.write", key="k-50")
    pendingCommand = await _enqueue(dbSession, instance, steps[1], commandType="po.write", key="k-51")
    executor = _executor(maxAttempts=3)

    # ⚠️ 失败路径会 rollback ⇒ ORM 实例被 expire ⇒ 先取标量。
    instanceId = instance.id
    okId = okCommand.id
    pendingId = pendingCommand.id
    skippedStepId = steps[1].id

    await executor.execute(dbSession, okId, workerId="worker-1")
    await executor.compensateInstance(dbSession, instanceId, reason="人工补偿")

    commandRow = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == pendingId))
    ).scalar_one()
    assert commandRow.status == "CANCELLED"

    stepRow = (
        await dbSession.execute(select(WorkflowStep).where(WorkflowStep.id == skippedStepId))
    ).scalar_one()
    assert stepRow.status == "SKIPPED"

async def test_补偿已成功的步骤仍记COMPENSEDED(dbSession, definition):
    """补偿 ⇒ **已执行过**的步骤记 COMPENSATED（不是 SKIPPED）。

    两条语义不可混：命令跑过并被回滚 ⇒ COMPENSATED；命令没跑 ⇒ SKIPPED。
    """

    instance, steps = await _instanceWithRunningSteps(
        dbSession, definition, idempotencyKey="comp-both", count=2
    )
    okCommand = await _enqueue(dbSession, instance, steps[0], commandType="po.write", key="k-52")
    await _enqueue(dbSession, instance, steps[1], commandType="po.write", key="k-53")
    executor = _executor(maxAttempts=3)

    instanceId = instance.id
    okId = okCommand.id
    okStepId = steps[0].id

    await executor.execute(dbSession, okId, workerId="worker-1")
    await executor.compensateInstance(dbSession, instanceId, reason="人工补偿")

    stepRow = (
        await dbSession.execute(select(WorkflowStep).where(WorkflowStep.id == okStepId))
    ).scalar_one()
    assert stepRow.status == "COMPENSATED"


# =============================================================================
# R11-F：失败收敛**不得**把 claim 一起回滚
# =============================================================================


# =============================================================================
# R12-M1：补偿审计必须记录**补偿前**实例的真实状态
# =============================================================================


async def test_补偿审计记录补偿前的真实实例状态(dbSession, definition):
    """补偿审计的 ``before`` 必须是**补偿前**实例的真实状态。

    回归点：补偿常发生在实例还 ``RUNNING`` 时（人工补偿 / 命令成功后驱动失败）
    ⇒ 审计若写死 ``FAILED`` ⇒ 审计声称「补偿前实例已失败」，而库里它当时
    还在跑 ⇒ **假审计**（审计与事实不符 = 审计没有价值）。
    """

    instance, steps = await _instanceWithSteps(
        dbSession,
        definition,
        idempotencyKey="comp-audit",
        codes=["write", "later"],
        runningCount=2,
    )
    okCommand = await _enqueue(dbSession, instance, steps[0], key="comp-audit-ok")
    executor = _executor()

    instanceId = instance.id
    okId = okCommand.id

    await executor.execute(dbSession, okId, workerId="worker-1")
    # 补偿前实例仍在 RUNNING（还有第二个步骤没跑完 ⇒ 不会被编排器收尾）。
    assert await _instanceStatus(dbSession, instanceId) == "RUNNING"

    await executor.compensateInstance(dbSession, instanceId, reason="人工补偿")

    rows = (
        (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "workflow_instance",
                    AuditLog.entity_id == instanceId,
                    AuditLog.action == "COMPENSATE",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].before_json == {"status": "RUNNING"}
    assert (rows[0].after_json or {}).get("status") == "COMPENSATED"
