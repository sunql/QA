"""P5 工作流 Worker（独立进程轮询 PG 表）单测（W4）。

被测纪律（W4 完成定义「Worker 集成（开关门控）」）：

1. **开关门控**：`ENABLE_WORKFLOW=false` ⇒ ``run()`` **立即返回**，
   **不建会话、不轮询**（回滚 = 关开关 + 停 Worker）。
2. **独立进程**：Worker 自己开会话、自己提交；不随 API 进程启动。
3. **领取即执行**：Worker 原子 claim（``SKIP LOCKED``）⇒ 交给执行器 ⇒
   命令落到终态；claim → 执行 → 终态在**一个事务**里。
4. **失败隔离**：一条命令失败**不打断**轮询（Worker 继续下一轮）。
5. **审批超时清扫**：超期未决的审批被标记 TIMEOUT（否则实例永远卡在等待审批）。

全程走**真实 PostgreSQL**（``workflow_*`` 表是真表、真事务、真行锁）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowDefinition,
)
from app.models.rbac import Role
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import (
    CommandHandlerRegistry,
    SimulatedCommandHandler,
)
from app.services.workflow_store import WorkflowStore
from app.workers.workflow_worker import WorkflowWorker

_store = WorkflowStore()

_MATRIX = [
    {
        "matrixCode": "po_medium_manager",
        "subjectType": "workflow_instance",
        "capabilityCode": "po_write",
        "riskLevel": "MEDIUM",
        "approverRole": "purchase_manager",
        "timeoutHours": 24,
    }
]


def _settings(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "enableWorkflow": True,
        "workflowPollSeconds": 0.01,
        "workflowLeaseSeconds": 60,
        "workflowMaxCommandAttempts": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ExplodingSessionFactory:
    """开关关闭时**绝不能**被调用（一旦调用即失败）。"""

    def __call__(self) -> Any:
        raise AssertionError("开关关闭时 worker 不得创建会话")


@pytest.fixture()
async def approverRole(dbSession) -> Role:
    row = Role(code="purchase_manager", name="采购经理")
    dbSession.add(row)
    await dbSession.commit()
    return row


@pytest.fixture()
async def definition(dbSession) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="po_write",
        version="v1",
        definition_json={
            "riskLevel": "MEDIUM",
            "steps": [{"code": "write", "order": 0}],
            "approvalMatrix": _MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _instanceWithStep(dbSession, definition, *, idempotencyKey: str = "worker-1"):
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


def _executor(handler=None, *, maxAttempts: int = 3) -> CommandExecutor:
    registry = CommandHandlerRegistry()
    registry.register("po.write", handler or SimulatedCommandHandler())
    return CommandExecutor(registry, maxAttempts=maxAttempts)


def _worker(factory, *, executor=None, settings=None, **overrides: Any) -> WorkflowWorker:
    return WorkflowWorker(
        settings or _settings(),
        factory,
        executor or _executor(),
        workerId="worker-test",
        **overrides,
    )


# =============================================================================
# 开关门控
# =============================================================================


async def test_开关关闭时worker不轮询():
    """回滚第一道闸：关开关 ⇒ 不建会话、不轮询。"""

    worker = _worker(_ExplodingSessionFactory(), settings=_settings(enableWorkflow=False))

    await worker.run()

    # 断言：没有抛异常即说明没碰会话工厂（工厂一旦被调用即 AssertionError）。


async def test_开关关闭时不执行任何命令(seedEngine, dbSession, definition):
    """关开关 ⇒ 已有待处理命令也不会被执行。"""

    factory, _ = seedEngine
    instance, step = await _instanceWithStep(dbSession, definition)
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=step.id,
        commandType="po.write",
        idempotencyKey="cmd-off",
        payload={},
    )
    await dbSession.commit()
    commandId = command.id

    worker = _worker(factory, settings=_settings(enableWorkflow=False))
    await worker.run()

    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "PENDING"


# =============================================================================
# 领取即执行
# =============================================================================


async def test_领取并执行待处理命令(seedEngine, dbSession, definition):
    factory, _ = seedEngine
    instance, step = await _instanceWithStep(dbSession, definition)
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=step.id,
        commandType="po.write",
        idempotencyKey="cmd-1",
        payload={},
    )
    await dbSession.commit()
    commandId = command.id

    worker = _worker(factory)
    claimed = await worker.runOnce()

    assert claimed is True
    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "SUCCEEDED"


async def test_没有可领取命令时本轮空转(seedEngine, dbSession):
    factory, _ = seedEngine

    worker = _worker(factory)

    assert await worker.runOnce() is False


async def test_命令失败不打断轮询(seedEngine, dbSession, definition):
    """失败隔离：适配器抛错 ⇒ 命令落 FAILED，Worker 照常返回（不抛异常）。"""

    factory, _ = seedEngine
    instance, step = await _instanceWithStep(dbSession, definition)
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=step.id,
        commandType="po.write",
        idempotencyKey="cmd-2",
        payload={"simulate": {"outcome": "fail", "message": "下游超时"}},
    )
    await dbSession.commit()
    commandId = command.id

    worker = _worker(factory)

    assert await worker.runOnce() is True
    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "FAILED"


# =============================================================================
# 审批超时清扫
# =============================================================================


async def _pendingApproval(dbSession, definition, *, dueInDays: int) -> int:
    """建一条待审批任务（``dueInDays`` 为负 ⇒ 已超期）。"""

    from datetime import timedelta

    from app.services.workflow_approval_service import WorkflowApprovalService

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="approval-instance",
        requestJson={},
        actor="submitter",
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()

    tasks = await WorkflowApprovalService().requestApproval(
        dbSession,
        instanceId=instance.id,
        actor="submitter",
    )
    await dbSession.commit()
    taskId = tasks[0].id
    task = (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == taskId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    task.due_at = task.due_at + timedelta(days=dueInDays)
    await dbSession.commit()
    return taskId


async def test_超期审批被清扫为TIMEOUT(seedEngine, dbSession, definition, approverRole):
    factory, _ = seedEngine
    taskId = await _pendingApproval(dbSession, definition, dueInDays=-1)

    worker = _worker(factory)
    swept = await worker.sweepApprovals(force=True)

    assert swept == 1
    row = (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == taskId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "TIMEOUT"


async def test_未到期审批不被清扫(seedEngine, dbSession, definition, approverRole):
    factory, _ = seedEngine
    taskId = await _pendingApproval(dbSession, definition, dueInDays=1)

    worker = _worker(factory)
    swept = await worker.sweepApprovals(force=True)

    assert swept == 0
    row = (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == taskId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "PENDING"


async def test_清扫按节流执行(seedEngine, dbSession, definition, approverRole):
    """节流：节流窗口内不重复扫（避免每轮都打一次 DB）。"""

    factory, _ = seedEngine
    await _pendingApproval(dbSession, definition, dueInDays=-1)

    worker = _worker(factory, approvalSweepSeconds=600)

    assert await worker.sweepApprovals(force=True) == 1
    assert await worker.sweepApprovals() == 0
