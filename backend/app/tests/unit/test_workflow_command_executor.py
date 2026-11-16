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

    assert outcome.status == "FAILED"
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

    assert outcome.status == "FAILED"
    assert outcome.isTerminal is True


async def test_未知命令类型失败关闭(dbSession, definition):
    """没注册适配器 ⇒ 终态失败（不放行、不无限重试）。"""

    instance, step = await _instanceWithStep(dbSession, definition)
    command = await _enqueue(dbSession, instance, step, commandType="po.unknown", key="k-7")
    executor = _executor()

    outcome = await executor.execute(dbSession, command.id, workerId="worker-1")

    assert outcome.status == "FAILED"
    assert outcome.isTerminal is True
    row = (
        await dbSession.execute(select(BusinessCommand).where(BusinessCommand.id == command.id))
    ).scalar_one()
    assert row.status == "FAILED"


# =============================================================================
# 补偿
# =============================================================================


async def test_终态失败触发同实例成功命令的补偿(dbSession, definition):
    """W3 完成定义：注入失败后补偿执行。"""

    instance, step = await _instanceWithStep(dbSession, definition)
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

    assert outcome.status == "FAILED"
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
    executor = _executor(maxAttempts=1)

    # ⚠️ 执行失败会 rollback ⇒ ORM 实例被 expire ⇒ 先取 id 再执行。
    badId = bad.id
    goodId = good.id

    failed = await executor.execute(dbSession, badId, workerId="worker-1")
    succeeded = await executor.execute(dbSession, goodId, workerId="worker-1")

    assert failed.status == "FAILED"
    assert succeeded.status == "SUCCEEDED"
