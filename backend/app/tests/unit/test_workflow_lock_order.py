"""P5 **锁序守卫**（死锁的机械防线）。

为什么需要这道守卫：并发路径各自给行加锁 ⇒ 若两条路径的加锁顺序**相反** ⇒
``40P01``（死锁）⇒ 现场表现是「偶发事务中止」⇒ 极难定位、极难复现。

本守卫把**全局锁序**钉成一条线：

    business_command（业务命令） → workflow_instance（实例）
        → approval_task（审批任务） → workflow_step（步骤）

并用**数据库真正收到的加锁顺序**（``FOR UPDATE`` 在 SQL 里出现的顺序）验证
四条并发路径：

1. **取消**（``WorkflowService.cancelInstance``）⇒ 命令 → 实例 → 任务 → 步骤；
2. **审批决策**（``WorkflowApprovalService.decideApproval``）⇒ 实例 → 任务；
3. **审批超时清扫**（``sweepTimedOutApprovals``）⇒ 实例 → 任务；
4. **命令执行 / 补偿**（``CommandExecutor``）⇒ 命令 → 步骤。

⚠️ **为什么不靠读代码判断**：读代码只能证明「我以为的顺序」；这里抓的是
**发到 PostgreSQL 的真实 SQL** ⇒ 中间 helper、隐式锁、间接调用都算得进去
⇒ 结论是**实测**，不是推测。

⚠️ **重复加锁不算违规**：同一条路径可能**再次**锁住已持有的表（例如取消路径
在锁完审批任务后又去锁命令行）⇒ 那不是新的锁 ⇒ 判定口径取「每张表**首次**
出现的顺序」。
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator

import pytest
from sqlalchemy import event

from app.domain.workflow_models import WorkflowDefinition
from app.models.rbac import Role
from app.services.workflow_approval_service import WorkflowApprovalService
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import (
    CommandHandlerRegistry,
    SimulatedCommandHandler,
)
from app.services.workflow_service import WorkflowService
from app.services.workflow_store import WorkflowStore

_store = WorkflowStore()
_service = WorkflowService()
_approval = WorkflowApprovalService()

# **全局锁序**（唯一一条线）：命令 → 实例 → 审批任务 → 步骤。
GLOBAL_LOCK_ORDER = (
    "business_command",
    "workflow_instance",
    "approval_task",
    "workflow_step",
)

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

_TABLE_IN_STATEMENT = re.compile(r"FROM\s+([a-z_]+)", re.IGNORECASE)


def _boundIntValues(params: object) -> set[int]:
    """从编译参数里取出所有整数（``compiled_parameters`` 可能是 dict 或 list[dict]）。"""

    if isinstance(params, dict):
        candidates: list[object] = list(params.values())
    elif isinstance(params, (list, tuple)):
        candidates = [
            value
            for item in params
            if isinstance(item, dict)
            for value in item.values()
        ]
    else:
        return set()
    return {value for value in candidates if isinstance(value, int) and not isinstance(value, bool)}


@contextlib.contextmanager
def _captureLockOrder(session, *, watchedIds: set[int]) -> Iterator[list[list[str]]]:
    """抓「数据库真正收到的加锁顺序」，**按事务分段**。

    ⚠️ **为什么按事务分段**：死锁是**同一时刻**两个事务互相等 ⇒ 判定单位
    必须是「**一个事务内**的加锁顺序」⇒ 跨事务拼一条序列没有意义
    （例：执行器的事务一锁「命令 + 步骤」、事务二锁「实例」⇒ 拼起来是
    「命令 → 步骤 → 实例」，但两个事务各自都合法）。

    ⚠️ **按 id 过滤**：事件挂在 **engine** 上（整个测试进程共享）⇒ 并行跑
    别的用例时它们的 SQL 也会流进来 ⇒ 只保留**参数里出现本次关注 id** 的语句
    ⇒ 别的用例的语句被挡在外面（id 来自序列 ⇒ 不会撞）。
    """

    engine = session.get_bind()
    syncSession = session.sync_session
    segments: list[list[str]] = []
    current: list[str] = []

    def _closeSegment() -> None:
        nonlocal current
        if current:
            segments.append(current)
            current = []

    def _onBegin(session_, transaction, connection) -> None:
        _closeSegment()

    def _onEnd(session_) -> None:
        _closeSegment()

    def _onCursorExecute(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" not in statement.upper():
            return
        match = _TABLE_IN_STATEMENT.search(statement)
        if match is None:
            return
        table = match.group(1).lower()
        if table not in GLOBAL_LOCK_ORDER:
            return
        try:
            params = context.compiled_parameters if context is not None else None
            values = _boundIntValues(params)
        except Exception:  # noqa: BLE001 —— 取不到参数就不当作本次的语句
            return
        if not (values & watchedIds):
            return
        current.append(table)

    event.listen(engine, "before_cursor_execute", _onCursorExecute)
    event.listen(syncSession, "after_begin", _onBegin)
    event.listen(syncSession, "after_commit", _onEnd)
    event.listen(syncSession, "after_rollback", _onEnd)
    try:
        yield segments
    finally:
        event.remove(engine, "before_cursor_execute", _onCursorExecute)
        event.remove(syncSession, "after_begin", _onBegin)
        event.remove(syncSession, "after_commit", _onEnd)
        event.remove(syncSession, "after_rollback", _onEnd)
        _closeSegment()


def _assertRespectsGlobalOrder(segments: list[list[str]], pathLabel: str) -> None:
    """判定：**每个事务内**的加锁顺序都必须是全局锁序的子序列。"""

    assert segments, f"{pathLabel}：一条 FOR UPDATE 都没抓到 ⇒ 守卫形同虚设"
    for segment in segments:
        unknown = [table for table in segment if table not in GLOBAL_LOCK_ORDER]
        assert unknown == [], f"{pathLabel}：出现未登记的加锁表 {unknown}"
        # 重复加锁不是新的锁 ⇒ 只看每张表**首次**出现的顺序。
        firstSeen = list(dict.fromkeys(segment))
        indexes = [GLOBAL_LOCK_ORDER.index(table) for table in firstSeen]
        assert indexes == sorted(indexes), (
            f"{pathLabel}：**同一事务内**锁序违反全局序 {GLOBAL_LOCK_ORDER}"
            f" ⇒ 实测 {firstSeen} ⇒ 40P01 死锁风险"
        )


@pytest.fixture()
async def approverRole(dbSession) -> Role:
    row = Role(code="purchase_manager", name="采购经理")
    dbSession.add(row)
    await dbSession.commit()
    return row


@pytest.fixture()
async def definition(dbSession, approverRole) -> WorkflowDefinition:
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


async def _instanceWithCommandAndApproval(dbSession, definition, *, key: str):
    """建一个「有待审批任务 + 有待执行命令」的实例。

    ⚠️ 必须**同时**有审批任务与命令：取消路径才会依次锁到三张表
    （命令 → 实例 → 任务）⇒ 少一样就测不出锁序。
    """

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey=key,
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

    tasks = await _approval.requestApproval(
        dbSession,
        instanceId=instance.id,
        actor="submitter",
    )
    await dbSession.commit()

    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=steps[0].id,
        commandType="po.write",
        idempotencyKey=f"{key}-cmd",
        payload={},
    )
    await dbSession.commit()
    return instance, steps[0], tasks[0], command


# =============================================================================
# 取消路径
# =============================================================================


async def test_取消路径的锁序遵循全局序(dbSession, definition):
    """取消：命令 → 实例 → 任务 → 步骤（**不得**反过来）。"""

    instance, _step, task, command = await _instanceWithCommandAndApproval(
        dbSession, definition, key="lock-cancel"
    )
    watched = {instance.id, task.id, command.id}

    with _captureLockOrder(dbSession, watchedIds=watched) as captured:
        await _service.cancelInstance(
            dbSession,
            instanceId=instance.id,
            actor="submitter",
            isRealIdentity=True,
            reason="锁序取证",
        )
        await dbSession.commit()

    _assertRespectsGlobalOrder(captured, "取消路径")


async def test_取消路径先锁命令再锁实例(dbSession, definition):
    """取消必须**先锁命令**：Worker 的 claim 也锁命令 ⇒ 顺序相反即互锁。"""

    instance, _step, task, command = await _instanceWithCommandAndApproval(
        dbSession, definition, key="lock-cancel-order"
    )
    watched = {instance.id, task.id, command.id}

    with _captureLockOrder(dbSession, watchedIds=watched) as segments:
        await _service.cancelInstance(
            dbSession,
            instanceId=instance.id,
            actor="submitter",
            isRealIdentity=True,
        )
        await dbSession.commit()

    captured = [table for segment in segments for table in segment]
    firstSeen = list(dict.fromkeys(captured))
    assert "business_command" in firstSeen, f"取消路径没有锁命令行：{captured}"
    assert "workflow_instance" in firstSeen, f"取消路径没有锁实例行：{captured}"
    assert firstSeen.index("business_command") < firstSeen.index("workflow_instance"), (
        f"取消路径锁序反了（先实例后命令）：{firstSeen}"
    )


# =============================================================================
# 审批决策路径
# =============================================================================


async def test_审批决策路径的锁序遵循全局序(dbSession, definition):
    """审批决策：实例 → 任务（**不得**先锁任务）。"""

    instance, _step, task, _command = await _instanceWithCommandAndApproval(
        dbSession, definition, key="lock-decide"
    )
    watched = {instance.id, task.id}

    with _captureLockOrder(dbSession, watchedIds=watched) as captured:
        await _approval.decideApproval(
            dbSession,
            taskId=task.id,
            decision="APPROVE",
            actor="manager",
            roles=["purchase_manager"],
        )
        await dbSession.commit()

    _assertRespectsGlobalOrder(captured, "审批决策路径")
    flat = [table for segment in captured for table in segment]
    firstSeen = list(dict.fromkeys(flat))
    assert firstSeen.index("workflow_instance") < firstSeen.index("approval_task"), (
        f"审批决策路径锁序反了（先任务后实例）：{firstSeen}"
    )


# =============================================================================
# 审批超时清扫路径
# =============================================================================


async def test_超时清扫路径的锁序遵循全局序(dbSession, definition):
    """超时清扫：实例 → 任务（与决策路径一致 ⇒ 两条路径不会互锁）。"""

    from datetime import timedelta

    instance, _step, task, _command = await _instanceWithCommandAndApproval(
        dbSession, definition, key="lock-sweep"
    )
    taskRow = task
    taskRow.due_at = taskRow.due_at - timedelta(days=1)
    await dbSession.commit()
    watched = {instance.id, task.id}

    with _captureLockOrder(dbSession, watchedIds=watched) as captured:
        await _approval.sweepTimedOutApprovals(dbSession)
        await dbSession.commit()

    _assertRespectsGlobalOrder(captured, "超时清扫路径")
    flat = [table for segment in captured for table in segment]
    firstSeen = list(dict.fromkeys(flat))
    assert firstSeen.index("workflow_instance") < firstSeen.index("approval_task"), (
        f"超时清扫路径锁序反了（先任务后实例）：{firstSeen}"
    )


# =============================================================================
# 命令执行路径
# =============================================================================


async def test_命令执行路径的锁序遵循全局序(dbSession, definition):
    """命令执行：命令 → 步骤（步骤锁在命令锁之后）。"""

    instance, step, _task, command = await _instanceWithCommandAndApproval(
        dbSession, definition, key="lock-exec"
    )
    watched = {instance.id, step.id, command.id}

    registry = CommandHandlerRegistry()
    registry.register("po.write", SimulatedCommandHandler())
    executor = CommandExecutor(registry, maxAttempts=3)

    with _captureLockOrder(dbSession, watchedIds=watched) as captured:
        await executor.execute(dbSession, command.id, workerId="worker-lock")
        await dbSession.commit()

    _assertRespectsGlobalOrder(captured, "命令执行路径")
