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
from app.workers.workflow_worker import (
    WORKER_ID_MAX_LEN,
    WorkflowWorker,
    _defaultWorkerId,
    buildCommandExecutor,
)

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
# 轮询隔离（清扫 vs 执行）
# =============================================================================


async def test_清扫失败不阻断命令执行(seedEngine, dbSession, definition):
    """清扫是**次要**职责：它炸了不能拖住**主要**职责（执行命令）。

    早期实现把两者放在**同一个** try 里 ⇒ 清扫（审批超时）一失败，命令就再也
    领不到 ⇒ 外部写永久停摆，而现场日志只有一条「轮询失败」。
    """

    factory, _ = seedEngine
    instance, step = await _instanceWithStep(dbSession, definition)
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=step.id,
        commandType="po.write",
        idempotencyKey="cmd-sweep-boom",
        payload={},
    )
    await dbSession.commit()
    commandId = command.id

    class _ExplodingApproval:
        async def sweepTimedOutApprovals(self, session: Any) -> int:
            raise RuntimeError("审批表查询失败（测试注入）")

    worker = _worker(factory, approval=_ExplodingApproval())

    await worker._pollOnce()

    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "SUCCEEDED"


# =============================================================================
# workerId 列宽
# =============================================================================


def test_死配置已删除():
    """``WORKFLOW_LEASE_SECONDS`` **无人读** ⇒ 死配置 ⇒ 删掉。

    留着它 ⇒ 运维在 ``.env`` 里配了租约时长，以为「命令超时会被回收」，
    实际没有任何代码读它 ⇒ 误解 ⇒ 真出事时按错误的心智模型排查。

    ⚠️ 命令的「在飞」状态由 claim 的**条件 UPDATE**（``PENDING → EXECUTING``）
    原子化，不需要租约超时兜底。
    """

    from app.config import Settings

    assert "workflowLeaseSeconds" not in Settings.model_fields


def test_workerId被截到executed_by列宽():
    """``business_command.executed_by`` 是 ``String(64)`` ⇒ workerId 超长会写库失败。

    ⚠️ 早期实现截到 128（按 ``lease_owner`` 的列宽）⇒ 主机名一长就**写不进**
    ``executed_by`` ⇒ claim 直接报错 ⇒ 命令永远执行不了。
    """

    assert WORKER_ID_MAX_LEN == 64

    worker = WorkflowWorker(
        _settings(),
        _ExplodingSessionFactory(),
        _executor(),
        workerId="w" * 300,
    )
    assert len(worker._workerId) == WORKER_ID_MAX_LEN

    assert len(_defaultWorkerId()) <= WORKER_ID_MAX_LEN


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


# =============================================================================
# R11-H：一轮必须**排空**待执行命令（吞吐不能被轮询间隔压死）
# =============================================================================


async def test_一轮排空多条待执行命令(seedEngine, dbSession, definition):
    """Worker 一轮把「未领取」的命令**排空**，不是只领一条。

    回归点：``runOnce`` 一次只领一条 ⇒ 每轮只处理 1 条 ⇒ 吞吐被
    ``workflowPollSeconds``（默认 2s）压死 ⇒ 约 30 条/分钟 ⇒
    实例越多越追不上。
    """

    factory, _ = seedEngine
    commandIds: list[int] = []
    for index in range(3):
        instance, step = await _instanceWithStep(
            dbSession, definition, idempotencyKey=f"worker-drain-{index}"
        )
        command, _ = await _store.enqueueCommand(
            dbSession,
            instanceId=instance.id,
            stepId=step.id,
            commandType="po.write",
            idempotencyKey=f"cmd-drain-{index}",
            payload={},
        )
        await dbSession.commit()
        commandIds.append(command.id)

    worker = _worker(factory)

    assert await worker.runOnce() is True

    rows = (
        (
            await dbSession.execute(
                select(BusinessCommand)
                .where(BusinessCommand.id.in_(commandIds))
                .order_by(BusinessCommand.id)
                # ⚠️ 必须 populate_existing：dbSession 的 identity map 里**已经**
                # 有这几条命令（入队时读过一次）⇒ 不刷新就会拿到**旧的**
                # PENDING ⇒ 假失败（Worker 其实已经把三条都执行完了）。
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert [row.status for row in rows] == ["SUCCEEDED"] * 3


async def test_一轮排空不热转可重试失败(seedEngine, dbSession, definition):
    """可重试失败的命令**每轮只被领一次**（重试要有节奏，不在同轮里热转）。

    回归点：排空若把 ``FAILED``（可重试）也当普通候选反复领 ⇒ 同一条命令在
    一轮里被领 ``maxAttempts`` 次 ⇒ 重试没有退避 ⇒ 毒丸命令把 worker 烧满。
    """

    factory, _ = seedEngine
    instance, step = await _instanceWithStep(
        dbSession, definition, idempotencyKey="worker-retry"
    )
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=step.id,
        commandType="po.write",
        idempotencyKey="cmd-retry",
        payload={"simulate": {"outcome": "fail", "message": "下游超时"}},
    )
    await dbSession.commit()
    commandId = command.id

    worker = _worker(factory)
    await worker.runOnce()

    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    # 一轮只试一次（不是把 maxAttempts 用光）
    assert row.status == "FAILED"
    assert row.attempts == 1


# =============================================================================
# R12-M3 / R12-M8：装配口径 + 一轮的批量上限
# =============================================================================


def test_组装根把设置传给执行器():
    """最大尝试次数与适配器超时都从**设置**来 ⇒ 必须**一处装配**。

    回归点：这两项在执行器里被用到**两处**（领取过滤条件 / 失败终态判定）
    ⇒ 各读各的配置 ⇒ 改了一个忘了另一个 ⇒ 领取口径与终态口径打架
    （命令领得到却永远不判终态 ⇒ 无限重试）。
    """

    settings = _settings(
        workflowMaxCommandAttempts=7,
        workflowCommandTimeoutSeconds=12.5,
    )
    registry = CommandHandlerRegistry()
    registry.register("po.write", SimulatedCommandHandler())

    executor = buildCommandExecutor(settings, registry)

    assert executor.maxAttempts == 7
    assert executor.commandTimeoutSeconds == 12.5


async def test_领取过滤用执行器的最大尝试次数(seedEngine, dbSession, monkeypatch):
    """领取条件必须用**执行器**的 ``maxAttempts``（而不是设置里的另一份）。

    回归点：Worker 若自读 ``settings.workflowMaxCommandAttempts``，而执行器
    用的是别处装配的值 ⇒ 两处口径可能不同 ⇒ 「领得到却永远不判终态」。
    """

    factory, _ = seedEngine
    captured: dict[str, Any] = {}

    async def _patchedClaim(self, session, *, workerId, maxAttempts=1, includeRetryable=True):
        captured["maxAttempts"] = maxAttempts
        return None

    monkeypatch.setattr(WorkflowStore, "claimNextCommand", _patchedClaim)

    registry = CommandHandlerRegistry()
    registry.register("po.write", SimulatedCommandHandler())
    executor = CommandExecutor(registry, maxAttempts=5)
    # 设置里故意给**不同的**值 ⇒ 若 Worker 自读设置，这里就会取到 3。
    worker = WorkflowWorker(
        _settings(workflowMaxCommandAttempts=3),
        factory,
        executor,
        workerId="worker-test",
    )

    await worker.runOnce()

    assert captured["maxAttempts"] == 5


async def test_一轮排空不超过批量上限(seedEngine, dbSession, definition):
    """一轮**总共**最多领 ``drainBatchSize`` 条（**含第一次领取**）。

    回归点：早期实现是「第一次 + 排空 N 条」= N+1 条 ⇒ 批量上限形同虚设
    （宣称的 50 实际是 51 ⇒ 上限失真 ⇒ 批量大时一轮跑得比预期久）。
    """

    factory, _ = seedEngine
    commandIds: list[int] = []
    for index in range(3):
        instance, step = await _instanceWithStep(
            dbSession, definition, idempotencyKey=f"worker-cap-{index}"
        )
        command, _ = await _store.enqueueCommand(
            dbSession,
            instanceId=instance.id,
            stepId=step.id,
            commandType="po.write",
            idempotencyKey=f"cmd-cap-{index}",
            payload={},
        )
        await dbSession.commit()
        commandIds.append(command.id)

    worker = _worker(factory, drainBatchSize=2)
    assert await worker.runOnce() is True

    rows = (
        (
            await dbSession.execute(
                select(BusinessCommand)
                .where(BusinessCommand.id.in_(commandIds))
                .order_by(BusinessCommand.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    statuses = [row.status for row in rows]
    # 只处理 2 条（不是 3 条）⇒ 剩下的下一轮再来。
    assert statuses.count("SUCCEEDED") == 2
    assert statuses.count("PENDING") == 1
