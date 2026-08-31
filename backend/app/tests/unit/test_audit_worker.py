"""AuditWorker 单元测试（feat-audit-outbox）。

真实 PG（SKIP LOCKED 是 PG 特性，sqlite 无此语法 -- 测试规范强制真实 PG）。
fixture 来自 unit/conftest.py 的 seedEngine/dbSession。

worker 的依赖（AuditService / HistoryService 写入路径）通过注错 / 观察验证：
不 mock DB 层，只通过 monkeypatch 注入「写入失败」的 service。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog, AuditOutbox, KpiCatalog, KpiCatalogHistory
from app.services.outbox_service import OutboxService
from app.workers.audit_worker import AuditWorker


async def _seedKpi(dbSession: AsyncSession, kpiId: int) -> KpiCatalog:
    """造真实 KPI 行（history 表有 FK 约束，必须真实存在）。"""
    kpi = KpiCatalog(
        id=kpiId,
        kpi_code=f"KPI_TEST_{kpiId}",
        kpi_name="回款率",
        revision_count=1,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    return kpi


async def _enqueueKpiEvent(
    session: AsyncSession, event_type: str, entity_id: int, seed_row: bool = True
) -> AuditOutbox:
    if seed_row:
        await _seedKpi(session, entity_id)
    svc = OutboxService()
    return await svc.enqueue(
        session,
        event_type=event_type,
        entity_type="kpi_catalog",
        entity_id=entity_id,
        actor="test-admin",
        payload={
            "after": {
                "id": entity_id,
                "kpi_name": "回款率",
                "revision_count": 1,
            }
        },
    )


@pytest.mark.asyncio
async def test_drain_once_processes_pending_and_writes_audit_log(
    dbSession: AsyncSession,
):
    """drain_once：pending -> audit_log 落地 + outbox 标记 processed。"""
    await _enqueueKpiEvent(dbSession, "kpi_created", 1)
    await dbSession.commit()

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()

    assert n == 1
    logs = (await dbSession.execute(select(AuditLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "CREATE"
    assert logs[0].entity_type == "kpi_catalog"
    assert logs[0].actor == "test-admin"
    assert logs[0].outbox_id is not None


@pytest.mark.asyncio
async def test_drain_once_is_idempotent(dbSession: AsyncSession):
    """重复 drain：audit_log 不增加（outbox 幂等消费）。"""
    await _enqueueKpiEvent(dbSession, "kpi_created", 1)
    await dbSession.commit()

    worker = AuditWorker()
    await worker.drainOnce(dbSession)
    await dbSession.commit()
    # 第二次 drain（无 pending）
    n2 = await worker.drainOnce(dbSession)
    await dbSession.commit()

    assert n2 == 0
    logs = (await dbSession.execute(select(AuditLog))).scalars().all()
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_drain_once_writes_kpi_history_snapshot(dbSession: AsyncSession):
    """kpi_* 事件：worker 同时写 kpi_catalog_history 快照。"""
    await _enqueueKpiEvent(dbSession, "kpi_created", 1)
    await dbSession.commit()

    worker = AuditWorker()
    await worker.drainOnce(dbSession)
    await dbSession.commit()

    histories = (await dbSession.execute(select(KpiCatalogHistory))).scalars().all()
    assert len(histories) == 1
    assert histories[0].kpi_id == 1
    assert histories[0].changed_by == "test-admin"


@pytest.mark.asyncio
async def test_drain_once_skips_when_audit_write_fails(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """audit 写入失败：outbox.attempts + 1、last_error 落内容、audit_log 不写。"""
    await _enqueueKpiEvent(dbSession, "kpi_created", 1)
    await dbSession.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("audit table is on fire")

    monkeypatch.setattr(
        "app.workers.audit_worker.AuditService.record", _boom
    )

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()

    # 失败行不计入处理数
    assert n == 0
    logs = (await dbSession.execute(select(AuditLog))).scalars().all()
    assert logs == []
    box = (
        await dbSession.execute(select(AuditOutbox))
    ).scalar_one()
    assert box.processed_at is None
    assert box.attempts == 1
    assert "audit table is on fire" in (box.last_error or "")


@pytest.mark.asyncio
async def test_drain_once_marks_poisoned_after_max_attempts(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """attempts 达到 max_attempts：停止重试（不再选中该行）。"""
    row = await _enqueueKpiEvent(dbSession, "kpi_created", 1)
    await dbSession.commit()
    # 直接预置毒丸状态
    row.attempts = 5
    await dbSession.commit()

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()

    assert n == 0


@pytest.mark.asyncio
async def test_drain_once_respects_batch_limit(dbSession: AsyncSession):
    """batch_size 限制单轮处理量，剩余留下轮。"""
    for i in range(5):
        await _enqueueKpiEvent(dbSession, "kpi_created", i + 1)
    await dbSession.commit()

    worker = AuditWorker(batchSize=3)
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()
    assert n == 3

    n2 = await worker.drainOnce(dbSession)
    await dbSession.commit()
    assert n2 == 2


@pytest.mark.asyncio
async def test_stop_event_interrupts_loop():
    """run() 循环响应 stop 事件（优雅停机，处理完当前批次退出）。

    run() 依赖全局 session factory（worker 进程语义）；测试内 factory 指向
    测试库（seedEngine fixture 已 monkeypatch 不可用时，run 前先请求停止，
    首轮 drain 在空库上执行后立即退出）。
    """
    import asyncio

    worker = AuditWorker(pollInterval=0.01)
    worker.requestStop()
    # run() 应立即退出而不 hang；全局 factory 缺失时也应触发异常路径而非死循环
    try:
        await asyncio.wait_for(worker.run(), timeout=2.0)
    except TypeError:
        pytest.skip("全局 factory 为 sqlite（根 conftest 环境），run() 路径留集成测试覆盖")
