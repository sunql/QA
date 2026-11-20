"""A4-0（0159）：审批任务的**被审对象三元组**幂等键 + 无实例审批。

背景：WP-ONT-AI A4 的「本体变更提案 · 四眼审核」要复用 P5 审批面，但提案
**没有工作流实例可挂** ⇒ ``approval_task.instance_id`` 必须可空（0159）。

⚠️ **幂等键不能含可空列** —— PG 里 NULL 互不相等 ⇒ 旧键
``(instance_id, matrix_code)`` 在提案路径上会**静默失效** ⇒ 同一提案的同一
矩阵条目能落两行 ⇒ 四眼失效。0159 把键换成
``(subject_type, subject_id, matrix_code)``（三列全 NOT NULL）。

本文件测**运行时行为**（真实 PostgreSQL + 真实表约束）：
迁移体的 DDL 形状由 ``test_workflow_models.py`` 负责（那条是「语法层」，
本条是「行为层」——只测 DDL 形状会漏掉「约束真的生效吗」）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.workflow_models import (
    ApprovalTask,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutbox,
)
from app.services.workflow_store import WorkflowStore
from app.services.workflow_store_base import EVENT_APPROVAL_REQUESTED

_PROPOSAL_SUBJECT_TYPE = "ontology_proposal"
_MATRIX_CODE = "four_eyes"


async def _makeInstance(dbSession: AsyncSession) -> WorkflowInstance:
    """造一个工作流实例（FK 目标）⇒ 用于验证**工作流路径**的幂等语义不变。"""

    definition = WorkflowDefinition(
        domain_code="a4_test",
        capability_code="proposal_review",
        version="v1",
        definition_json={},
        status="DRAFT",
        created_by="tester",
    )
    dbSession.add(definition)
    await dbSession.flush()
    instance = WorkflowInstance(
        definition_id=definition.id,
        idempotency_key="a4-idem-1",
        status="PENDING",
        request_json={},
        created_by="tester",
    )
    dbSession.add(instance)
    await dbSession.flush()
    return instance


async def _createTask(
    dbSession: AsyncSession,
    store: WorkflowStore,
    *,
    instanceId: int | None,
    subjectType: str,
    subjectId: str,
    matrixCode: str = _MATRIX_CODE,
) -> tuple[ApprovalTask, bool]:
    task, created = await store.createApprovalTask(
        dbSession,
        instanceId=instanceId,
        subjectType=subjectType,
        subjectId=subjectId,
        matrixCode=matrixCode,
        approverRole="reviewer",
        approverUser=None,
        dueAt=datetime.now(UTC) + timedelta(hours=24),
        createdBy="tester",
    )
    await dbSession.commit()
    return task, created


async def _taskRows(
    dbSession: AsyncSession, *, subjectId: str
) -> list[ApprovalTask]:
    rows = (
        (
            await dbSession.execute(
                select(ApprovalTask)
                .where(ApprovalTask.subject_id == subjectId)
                .order_by(ApprovalTask.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_提案路径审批任务不挂工作流实例(dbSession: AsyncSession):
    """A4-0 的核心放开：``instance_id`` 可空 ⇒ 提案路径能落审批任务。"""

    store = WorkflowStore()
    task, created = await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-1",
    )

    assert created is True
    assert task.instance_id is None
    rows = await _taskRows(dbSession, subjectId="prop-1")
    assert len(rows) == 1
    assert rows[0].instance_id is None


@pytest.mark.asyncio
async def test_同一提案同一矩阵条目重复发起只落一条(dbSession: AsyncSession):
    """⚠️ **旧键在这里会静默失效**（instance_id IS NULL ⇒ NULL 互不相等）。

    ⇒ 这条是 0159 换键的**存在理由**：没有它，同一提案能落两行 ⇒ 四眼失效。
    """

    store = WorkflowStore()
    first, firstCreated = await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-2",
    )
    second, secondCreated = await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-2",
    )

    assert firstCreated is True
    assert secondCreated is False, "重复发起不得再落一行"
    assert second.id == first.id
    rows = await _taskRows(dbSession, subjectId="prop-2")
    assert len(rows) == 1, f"同一提案的同一矩阵条目只能有一行，实得 {len(rows)} 行"


@pytest.mark.asyncio
async def test_不同提案同矩阵条目互不冲突(dbSession: AsyncSession):
    """幂等键含 subject_id ⇒ 两个提案各留一行（不能互相吞掉）。"""

    store = WorkflowStore()
    _task, createdA = await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-a",
    )
    _task, createdB = await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-b",
    )

    assert createdA is True
    assert createdB is True
    assert len(await _taskRows(dbSession, subjectId="prop-a")) == 1
    assert len(await _taskRows(dbSession, subjectId="prop-b")) == 1


@pytest.mark.asyncio
async def test_工作流路径幂等语义逐字不变(dbSession: AsyncSession):
    """工作流路径恒有 ``subject_id = str(instance.id)`` ⇒ 新旧键一一对应。"""

    store = WorkflowStore()
    instance = await _makeInstance(dbSession)
    await dbSession.commit()

    _task, firstCreated = await _createTask(
        dbSession,
        store,
        instanceId=instance.id,
        subjectType="workflow_instance",
        subjectId=str(instance.id),
    )
    _task, secondCreated = await _createTask(
        dbSession,
        store,
        instanceId=instance.id,
        subjectType="workflow_instance",
        subjectId=str(instance.id),
    )

    assert firstCreated is True
    assert secondCreated is False, "同一实例同一矩阵条目仍只落一条"
    rows = await _taskRows(dbSession, subjectId=str(instance.id))
    assert len(rows) == 1
    assert rows[0].instance_id == instance.id


@pytest.mark.asyncio
async def test_实例口径与被审对象口径互斥(dbSession: AsyncSession):
    """两条查询口径**不重叠**：实例口径看不到无实例的行，反之亦然。"""

    store = WorkflowStore()
    instance = await _makeInstance(dbSession)
    await dbSession.commit()

    await _createTask(
        dbSession,
        store,
        instanceId=instance.id,
        subjectType="workflow_instance",
        subjectId=str(instance.id),
    )
    await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-3",
    )

    instanceScoped = await store.listApprovalTasks(dbSession, instanceId=instance.id)
    assert [row.id for row in instanceScoped] == [
        row.id for row in await _taskRows(dbSession, subjectId=str(instance.id))
    ]
    assert all(row.instance_id == instance.id for row in instanceScoped)

    subjectScoped = await store.listApprovalTasksBySubject(
        dbSession,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-3",
    )
    assert len(subjectScoped) == 1
    assert subjectScoped[0].instance_id is None


@pytest.mark.asyncio
async def test_无实例任务的审批发起事件落outbox且实例为空(dbSession: AsyncSession):
    """事件仍要留痕（审计面不能因放开实例而丢）⇒ outbox 行 ``instance_id`` 可空。"""

    store = WorkflowStore()
    await _createTask(
        dbSession,
        store,
        instanceId=None,
        subjectType=_PROPOSAL_SUBJECT_TYPE,
        subjectId="prop-4",
    )

    events = (
        (
            await dbSession.execute(
                select(WorkflowOutbox)
                .where(WorkflowOutbox.event_type == EVENT_APPROVAL_REQUESTED)
                .order_by(WorkflowOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    assert events, "审批发起事件必须留痕"
    assert events[-1].instance_id is None
    assert events[-1].payload["subjectType"] == _PROPOSAL_SUBJECT_TYPE
    assert events[-1].payload["subjectId"] == "prop-4"
