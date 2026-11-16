"""P5 Command 执行器（执行 / 重试 / 补偿 / 失败隔离）单测（W3）。

被测纪律（W3 完成定义「注入失败后补偿执行；新适配器不改核心」）：

1. **执行器只认适配器**：命令类型没注册适配器 ⇒ **失败关闭**（终态失败，不重试）。
2. **重试语义**：可重试失败 ⇒ 命令置 FAILED（可再次领取）；尝试次数耗尽 ⇒ 终态失败。
3. **补偿钩子**：终态失败 ⇒ 同实例内**已成功**的命令被补偿（逆序）⇒ 命令 COMPENSATED、
   实例 COMPENSATED。适配器不支持补偿 ⇒ 跳过（留痕，不假装补偿过）。
4. **失败隔离**：一条命令失败**不影响**其它命令（worker 主循环不被打断）。
5. **不重复执行**：已 SUCCEEDED 的命令再次被驱动 ⇒ SKIPPED（适配器不再被调用）。

全程走**真实 PostgreSQL**（`workflow_*` 表是真表、真事务）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.domain.workflow_models import (
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import (
    CommandExecutionError,
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


async def _instanceWithStep(dbSession, definition, *, idempotencyKey: str = "exec-1"):
    """建实例 + 一个步骤（步骤 RUNNING ⇒ 可被补偿）。"""

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
        steps=[{"code": "write", "order": 0}],
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    await _store.advanceStep(dbSession, steps[0].id, "RUNNING")
    await dbSession.commit()
    return instance, steps[0]


async def _instanceWithRunningSteps(dbSession, definition, *, idempotencyKey: str, count: int = 2):
    """建实例 + 多个步骤（**全部 RUNNING**）。

    为什么需要多步骤：单步骤实例在**第一条命令成功后**会被编排器收成 COMPLETED
    ⇒ 归属实例不再「活着」⇒ 后续命令按契约**不得**再被领取（已取消 / 已补偿 /
    已完成的实例不得再发起外部写）。⇒ 需要「实例保持 RUNNING」的场景（补偿、
    多命令并存）必须给它留一个未完成的步骤。
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
        steps=[{"code": f"write-{index}", "order": index} for index in range(count)],
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    for step in steps:
        await _store.advanceStep(dbSession, step.id, "RUNNING")
    await dbSession.commit()
    return instance, steps


async def _enqueue(dbSession, instance, step, *, commandType: str, key: str, payload: dict | None = None):
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


# =============================================================================
# 成功路径
# =============================================================================


async def test_成功执行写回结果(dbSession, definition):
    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-1")
    executor = _executor()

    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    assert outcome.status == "SUCCEEDED"
    assert outcome.isTerminal is True
    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == command.id))
    ).scalar_one()
    assert row.status == "SUCCEEDED"
    assert row.result["simulated"] is True


async def test_已成功的命令不重复执行(dbSession, definition):
    """幂等：再驱动一次已 SUCCEEDED 的命令 ⇒ SKIPPED，适配器不再被调用。"""

    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-2")
    handler = SimulatedCommandHandler()
    executor = _executor(handler)

    await executor.execute(dbSession, command.id, workerId="worker-1")
    callsAfterFirst = len(handler.calls)

    outcome = await executor.execute(dbSession, command.id, workerId="worker-2")

    assert outcome.status == "SKIPPED"
    assert len(handler.calls) == callsAfterFirst


# =============================================================================
# 失败与重试
# =============================================================================


async def test_可重试失败置FAILED且未终态(dbSession, definition):
    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-3",
        payload={"simulate": {"outcome": "fail", "message": "下游超时"}},
    )
    executor = _executor()

    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    assert outcome.status == "FAILED"
    assert outcome.isTerminal is False
    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == command.id))
    ).scalar_one()
    assert row.status == "FAILED"
    assert row.attempts == 1
    assert "下游超时" in (row.error_message or "")


async def test_再次驱动会累加尝试次数(dbSession, definition):
    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-4",
        payload={"simulate": {"outcome": "fail", "failTimes": 5}},
    )
    executor = _executor(maxAttempts=3)

    await executor.execute(dbSession, command.id, workerId="worker-1")
    await executor.execute(dbSession, command.id, workerId="worker-1")

    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == command.id))
    ).scalar_one()
    assert row.attempts == 2
    assert row.status == "FAILED"


async def test_尝试次数耗尽判终态失败(dbSession, definition):
    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-5",
        payload={"simulate": {"outcome": "fail", "failTimes": 9}},
    )
    executor = _executor(maxAttempts=2)

    await executor.execute(dbSession, command.id, workerId="worker-1")
    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    # 终态失败**落库**为 ABORTED（不是 FAILED）⇒ Worker 领不走（见 k-30 用例）
    assert outcome.status == "ABORTED"
    assert outcome.isTerminal is True


async def test_不可重试失败直接终态(dbSession, definition):
    """业务上不可重试 ⇒ 不再重试，直接终态。"""

    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-6")

    class _NonRetryable:
        async def execute(self, session, *, command):
            raise CommandExecutionError("参数非法", retryable=False)

    executor = _executor(_NonRetryable())

    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    assert outcome.status == "ABORTED"
    assert outcome.isTerminal is True


async def test_未知命令类型失败关闭(dbSession, definition):
    """没注册适配器 ⇒ 终态失败（不放行、不无限重试）。"""

    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.unknown", key="k-7")
    executor = _executor()

    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    assert outcome.status == "ABORTED"
    assert outcome.isTerminal is True
    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == command.id))
    ).scalar_one()
    assert row.status == "ABORTED"


# =============================================================================
# 终态必须**落库**（否则 attempts 未耗尽 ⇒ Worker 仍会重领）
# =============================================================================


async def test_终态失败落库为终态不再被领取(dbSession, definition):
    """不可重试失败 ⇒ 命令**落库**为终态（ABORTED）⇒ 不得再被领取。

    ⚠️ 回归点：终态此前只存在于**内存**（``outcome.isTerminal``）⇒ 库里仍是
    ``FAILED`` 且 ``attempts < maxAttempts`` ⇒ Worker 会把它当「可重试失败」
    再领一次 ⇒ 业务上不可重试的命令被无限重试。

    这里关掉补偿（``compensateOnFailure=False``）⇒ 实例仍是 RUNNING ⇒
    「领不到」只能由命令自身的终态保证（不是被实例状态顺带挡住）。
    """

    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-30")

    class _NonRetryable:
        async def execute(self, session, *, command):
            raise CommandExecutionError("参数非法", retryable=False)

    registry = CommandHandlerRegistry()
    registry.register("po.write", _NonRetryable())
    executor = CommandExecutor(registry, maxAttempts=3, compensateOnFailure=False)

    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    assert outcome.isTerminal is True
    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == command.id))
    ).scalar_one()
    assert row.status == "ABORTED"

    claimed = await _store.claimNextCommand(dbSession, workerId="worker-2", maxAttempts=3)
    assert claimed is None


async def test_补偿后实例的命令不再被领取(dbSession, definition):
    """补偿 ⇒ 实例终态 ⇒ 它的命令**不得**再被 Worker 领走（否则「补偿」只是纸面动作）。

    回归点：补偿前命令是 ``FAILED``（可重试）⇒ 旧实现只取消 ``PENDING``
    ⇒ ``FAILED`` 仍可被领取 ⇒ 已补偿的实例照样写外部系统。
    """

    instance, step = await _instanceWithStep(dbSession, definition)
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
    instance, steps = await _instanceWithRunningSteps(dbSession, definition, idempotencyKey="exec-comp")
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
    instance, steps = await _instanceWithRunningSteps(dbSession, definition, idempotencyKey="exec-mixed")
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


async def test_一条命令失败不影响其它命令(dbSession, definition):
    """失败隔离：一条命令失败**不打断**其它命令（worker 主循环不被打断）。

    ⚠️ 用 ``maxAttempts=3`` ⇒ 失败是**可重试**的 ⇒ 不触发补偿 ⇒
    姊妹命令照常执行（终态失败会触发补偿，见下一个用例）。
    """

    instance, step = await _instanceWithStep(dbSession, definition)
    bad = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-13",
        payload={"simulate": {"outcome": "fail"}},
    )
    good = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-14")
    executor = _executor(maxAttempts=3)

    # ⚠️ 执行失败会 rollback ⇒ ORM 实例被 expire ⇒ 先取 id 再执行。
    badId = bad.id
    goodId = good.id

    failed = await executor.execute(dbSession, badId, workerId="worker-1")
    succeeded = await executor.execute(dbSession, goodId, workerId="worker-1")

    assert failed.status == "FAILED"
    assert succeeded.status == "SUCCEEDED"


async def test_终态失败会取消同实例待执行命令(dbSession, definition):
    """补偿 ⇒ 同实例的**待执行命令**一并取消（否则「回滚」只是纸面动作）。

    终态失败会补偿整个实例 ⇒ 实例 COMPENSATED ⇒ Worker **不得**再领它的
    PENDING 命令去写外部系统。
    """

    instance, step = await _instanceWithStep(dbSession, definition)
    bad = await _enqueue(
        dbSession,
        instance,
        step,
        commandType="po.write",
        key="k-15",
        payload={"simulate": {"outcome": "fail"}},
    )
    good = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-16")
    executor = _executor(maxAttempts=1)

    badId = bad.id
    goodId = good.id

    await executor.execute(dbSession, badId, workerId="worker-1")

    goodRow = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == goodId))
    ).scalar_one()
    assert goodRow.status == "CANCELLED"

    # 取消后的命令**领不走**（Worker 不会再写外部系统）
    # ⚠️ maxAttempts=1 ⇒ 已耗尽次数的失败命令也不可领 ⇒ 领不到任何命令
    claimed = await _store.claimNextCommand(dbSession, workerId="worker-2", maxAttempts=1)
    assert claimed is None


# =============================================================================
# 成功后推进：步骤 → SUCCEEDED，实例 → COMPLETED
# =============================================================================


async def test_命令成功后步骤置SUCCEEDED(dbSession, definition):
    """命令成功 ⇒ 它所属的步骤 SUCCEEDED（否则步骤永远卡在 RUNNING）。"""

    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.write", key="k-20")
    executor = _executor()

    await executor.execute(dbSession, command.id, workerId="worker-1")

    stepRow = (
        await dbSession.execute(select(WorkflowStep).where(WorkflowStep.id == step.id))
    ).scalar_one()
    assert stepRow.status == "SUCCEEDED"


async def test_全部步骤成功后实例置COMPLETED(dbSession, definition):
    """所有步骤都成功后 ⇒ 实例 COMPLETED（实例的「完成」路径必须可达）。"""

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="k-21",
        requestJson={},
        actor="submitter",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "a", "order": 0}, {"code": "b", "order": 1}],
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    for step in steps:
        await _store.advanceStep(dbSession, step.id, "RUNNING")
    await dbSession.commit()

    commands = []
    for index, step in enumerate(steps):
        commands.append(
            await _enqueue(
                dbSession,
                instance,
                step,
                commandType="po.write",
                key=f"k-21-{index}",
            )
        )

    executor = _executor()
    for command in commands:
        await executor.execute(dbSession, command.id, workerId="worker-1")

    instanceRow = (
        await dbSession.execute(
            select(WorkflowInstance).where(WorkflowInstance.id == instance.id)
        )
    ).scalar_one()
    assert instanceRow.status == "COMPLETED"


async def test_仍有步骤未完成时实例不置COMPLETED(dbSession, definition):
    """只完成一步 ⇒ 实例**不得** COMPLETED（不能提前宣布完成）。"""

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="k-22",
        requestJson={},
        actor="submitter",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "a", "order": 0}, {"code": "b", "order": 1}],
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    for step in steps:
        await _store.advanceStep(dbSession, step.id, "RUNNING")
    await dbSession.commit()

    first = await _enqueue(dbSession, instance, steps[0], commandType="po.write", key="k-22-0")
    executor = _executor()
    await executor.execute(dbSession, first.id, workerId="worker-1")

    instanceRow = (
        await dbSession.execute(
            select(WorkflowInstance).where(WorkflowInstance.id == instance.id)
        )
    ).scalar_one()
    assert instanceRow.status == "RUNNING"
