"""P5 工作流存储层（幂等 / 行锁推进 / 条件 UPDATE claim）单测。

走**真实 PostgreSQL**（unit 夹具本来就提供真实 PG 会话，见
`app/tests/unit/conftest.py`）⇒ 不用 sqlite 内存库。

被测纪律：

1. **幂等**：同一 ``(definition_id, idempotency_key)`` 重复提交只落一行，
   第二次返回既有行（``created=False``）；``business_command`` 同理。
   ⇒ 用 ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` 实现 ⇒ 并发下也成立。
2. **行锁推进**：推进前在锁内**重读**当前状态（``populate_existing=True``），
   否则 identity map 里的陈旧值会让并发推进**丢步**。
3. **条件 UPDATE claim**：``WHERE id=? AND status='PENDING'`` ⇒ 同一命令
   只能被 claim 一次；``SKIP LOCKED`` 让多个 worker 各拿各的。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import (
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
)
from app.services.workflow_store import WorkflowStore

_store = WorkflowStore()


@pytest_asyncio.fixture()
async def definition(dbSession: AsyncSession) -> WorkflowDefinition:
    """一条 PUBLISHED 的定义（实例的父行）。"""

    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="purchase_order_approve",
        version="v1",
        definition_json={"steps": [{"code": "review", "order": 0}]},
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


# ---------------------------------------------------------------- 幂等


async def test_重复提交同一幂等键只落一行(dbSession: AsyncSession, definition):
    first, createdFirst = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="key-1",
        requestJson={"amount": 100},
        actor="tester",
    )
    await dbSession.commit()

    second, createdSecond = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="key-1",
        requestJson={"amount": 100},
        actor="tester",
    )
    await dbSession.commit()

    assert createdFirst is True
    assert createdSecond is False
    assert first.id == second.id

    rows = (
        (
            await dbSession.execute(
                select(WorkflowInstance).where(
                    WorkflowInstance.definition_id == definition.id,
                    WorkflowInstance.idempotency_key == "key-1",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_不同幂等键落不同实例(dbSession: AsyncSession, definition):
    first, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="key-a",
        requestJson={},
        actor="tester",
    )
    second, createdSecond = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="key-b",
        requestJson={},
        actor="tester",
    )
    await dbSession.commit()

    assert createdSecond is True
    assert first.id != second.id


async def test_并发提交同一幂等键只落一行(seedEngine, definition):
    """两个会话同时提交同一幂等键 ⇒ 只有一行（ON CONFLICT DO NOTHING）。"""

    factory, _engine = seedEngine
    results: list[tuple[int, bool]] = []

    async def _submit() -> None:
        async with factory() as session:
            instance, created = await _store.createInstance(
                session,
                definitionId=definition.id,
                idempotencyKey="race-key",
                requestJson={},
                actor="tester",
            )
            await session.commit()
            results.append((instance.id, created))

    await asyncio.gather(_submit(), _submit())

    assert len(results) == 2
    assert len({instanceId for instanceId, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


# ---------------------------------------------------------------- 推进


async def test_推进到新状态(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="advance-1",
        requestJson={},
        actor="tester",
    )
    await dbSession.commit()

    updated = await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()

    assert updated.status == "RUNNING"
    assert updated.id == instance.id


async def test_非法流转被拒且行未变(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="advance-bad",
        requestJson={},
        actor="tester",
    )
    await dbSession.commit()
    instanceId = instance.id

    with pytest.raises(ConflictError):
        await _store.advanceInstance(dbSession, instanceId, "COMPLETED")

    await dbSession.rollback()
    current = (
        await dbSession.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
    ).scalar_one()
    assert current.status == "PENDING"


async def test_推进不存在的实例报404(dbSession: AsyncSession):
    with pytest.raises(NotFoundError):
        await _store.advanceInstance(dbSession, 987654321, "RUNNING")


async def test_并发推进不丢步(seedEngine, definition):
    """陈旧的 identity map 不得让非法流转通过。

    场景：会话 A 先**无锁**读到实例（status=PENDING）⇒ A 推进到 RUNNING ⇒
    会话 B 把实例推进到 FAILED 并提交 ⇒ 会话 A 再推进到 COMPLETED。

    正确实现：锁内**重读**（``populate_existing=True``）⇒ 看到 FAILED ⇒
    FAILED → COMPLETED 非法 ⇒ 拒绝（且错误里的来源状态是**库里的最新值**）。

    ⚠️ 两道防线都要在：
    ① 锁内重读到**最新**状态 ⇒ 状态机当场拒（错误信息准确）；
    ② 写库时按「当前状态」条件 UPDATE ⇒ 即使①被绕过（陈旧值判定为合法），
       条件 UPDATE 命中 0 行 ⇒ 仍拒绝覆盖（不丢步）。
    断言 ``details["from"] == "FAILED"`` 把①钉住——否则②会静默兜底成
    「状态已被并发改动」这种没有上下文的错误。
    """

    factory, _engine = seedEngine
    async with factory() as setupSession:
        instance, _ = await _store.createInstance(
            setupSession,
            definitionId=definition.id,
            idempotencyKey="concurrent-advance",
            requestJson={},
            actor="tester",
        )
        await setupSession.commit()
        instanceId = instance.id

    # 会话 A：无锁读 ⇒ 进 identity map（陈旧值 = PENDING）
    async with factory() as sessionA:
        stale = await _store.getInstance(sessionA, instanceId)
        assert stale.status == "PENDING"
        await _store.advanceInstance(sessionA, instanceId, "RUNNING")
        await sessionA.commit()

        # 会话 B：推进到 FAILED 并提交
        async with factory() as sessionB:
            await _store.advanceInstance(sessionB, instanceId, "FAILED")
            await sessionB.commit()

        # 会话 A：仍持有 status=RUNNING 的陈旧对象 ⇒ 推进到 COMPLETED 必须被拒，
        # 且拒绝依据必须是**库里的最新状态 FAILED**
        with pytest.raises(ConflictError) as excinfo:
            await _store.advanceInstance(sessionA, instanceId, "COMPLETED")
        assert excinfo.value.details is not None
        assert excinfo.value.details["from"] == "FAILED"
        assert excinfo.value.details["to"] == "COMPLETED"
        await sessionA.rollback()

    async with factory() as verifySession:
        current = (
            await verifySession.execute(select(WorkflowInstance).where(WorkflowInstance.id == instanceId))
        ).scalar_one()
        assert current.status == "FAILED"


# ---------------------------------------------------------------- 步骤


async def test_创建步骤且实例内编码唯一(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="steps-1",
        requestJson={},
        actor="tester",
    )
    await dbSession.commit()

    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "review", "order": 0}, {"code": "approve", "order": 1}],
    )
    await dbSession.commit()

    assert [step.step_code for step in steps] == ["review", "approve"]
    assert [step.status for step in steps] == ["PENDING", "PENDING"]

    # 同一实例内步骤编码重复 ⇒ 拒绝（库层唯一约束是兜底）
    with pytest.raises(ConflictError):
        await _store.createSteps(
            dbSession,
            instanceId=instance.id,
            steps=[{"code": "review", "order": 2}],
        )
    await dbSession.rollback()


# ---------------------------------------------------------------- 命令


async def test_命令入队幂等(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="cmd-1",
        requestJson={},
        actor="tester",
    )
    await dbSession.commit()

    first, createdFirst = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=None,
        commandType="supplier.update",
        idempotencyKey="cmd-key-1",
        payload={"supplierId": 7},
    )
    await dbSession.commit()

    second, createdSecond = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=None,
        commandType="supplier.update",
        idempotencyKey="cmd-key-1",
        payload={"supplierId": 7},
    )
    await dbSession.commit()

    assert createdFirst is True
    assert createdSecond is False
    assert first.id == second.id


async def test_命令只能被claim一次(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="cmd-2",
        requestJson={},
        actor="tester",
    )
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=None,
        commandType="supplier.update",
        idempotencyKey="cmd-key-2",
        payload={},
    )
    await dbSession.commit()

    claimed = await _store.claimCommand(dbSession, command.id, workerId="worker-1")
    await dbSession.commit()
    assert claimed is not None
    assert claimed.status == "EXECUTING"

    again = await _store.claimCommand(dbSession, command.id, workerId="worker-2")
    await dbSession.commit()
    assert again is None


async def test_claimNext给不同worker不同命令(seedEngine, definition):
    factory, _engine = seedEngine
    async with factory() as setupSession:
        instance, _ = await _store.createInstance(
            setupSession,
            definitionId=definition.id,
            idempotencyKey="cmd-3",
            requestJson={},
            actor="tester",
        )
        for index in range(2):
            await _store.enqueueCommand(
                setupSession,
                instanceId=instance.id,
                stepId=None,
                commandType="supplier.update",
                idempotencyKey=f"cmd-key-3-{index}",
                payload={},
            )
        await setupSession.commit()

    async with factory() as sessionA:
        first = await _store.claimNextCommand(sessionA, workerId="worker-a")
        await sessionA.commit()
    async with factory() as sessionB:
        second = await _store.claimNextCommand(sessionB, workerId="worker-b")
        await sessionB.commit()

    assert first is not None
    assert second is not None
    assert first.id != second.id


async def test_命令完成走状态机(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="cmd-4",
        requestJson={},
        actor="tester",
    )
    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=None,
        commandType="supplier.update",
        idempotencyKey="cmd-key-4",
        payload={},
    )
    await dbSession.commit()

    await _store.claimCommand(dbSession, command.id, workerId="worker-1")
    await dbSession.commit()

    done = await _store.completeCommand(dbSession, command.id, "SUCCEEDED", result={"ok": True})
    await dbSession.commit()
    assert done.status == "SUCCEEDED"
    assert done.result == {"ok": True}

    with pytest.raises(ConflictError):
        await _store.completeCommand(dbSession, command.id, "EXECUTING")
    await dbSession.rollback()


async def test_步骤状态机拒绝非法流转(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="step-advance",
        requestJson={},
        actor="tester",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "review", "order": 0}],
    )
    await dbSession.commit()

    updated = await _store.advanceStep(dbSession, steps[0].id, "RUNNING")
    await dbSession.commit()
    assert updated.status == "RUNNING"

    with pytest.raises(ConflictError):
        await _store.advanceStep(dbSession, steps[0].id, "SKIPPED")
    await dbSession.rollback()


async def test_步骤不属于别的实例(dbSession: AsyncSession, definition):
    """步骤的实例归属必须与命令的实例一致 ⇒ 防止跨实例挂错。"""

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="step-owner",
        requestJson={},
        actor="tester",
    )
    other, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="step-owner-other",
        requestJson={},
        actor="tester",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "review", "order": 0}],
    )
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await _store.enqueueCommand(
            dbSession,
            instanceId=other.id,
            stepId=steps[0].id,
            commandType="supplier.update",
            idempotencyKey="cmd-mismatch",
            payload={},
        )


async def test_命令入队后可见于库(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="cmd-visible",
        requestJson={},
        actor="tester",
    )
    await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=None,
        commandType="supplier.update",
        idempotencyKey="cmd-key-visible",
        payload={"a": 1},
    )
    await dbSession.commit()

    rows = (
        (await dbSession.execute(select(BusinessCommand).where(BusinessCommand.instance_id == instance.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload == {"a": 1}
    assert rows[0].status == "PENDING"


async def test_步骤行可查(dbSession: AsyncSession, definition):
    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey="step-visible",
        requestJson={},
        actor="tester",
    )
    await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "review", "order": 0}],
    )
    await dbSession.commit()

    rows = (
        (await dbSession.execute(select(WorkflowStep).where(WorkflowStep.instance_id == instance.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
