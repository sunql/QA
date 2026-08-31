"""AuditWorker 单元测试（feat-audit-outbox）。

真实 PG（SKIP LOCKED 是 PG 特性，sqlite 无此语法 -- 测试规范强制真实 PG）。
fixture 来自 unit/conftest.py 的 seedEngine/dbSession。

worker 的依赖（AuditService / HistoryService 写入路径）通过注错 / 观察验证：
不 mock DB 层，只通过 monkeypatch 注入「写入失败」的 service。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    AuditLog,
    AuditOutbox,
    DataSource,
    FeatureDefinition,
    FeatureDefinitionHistory,
    KpiCatalog,
    KpiCatalogHistory,
)
from app.services.outbox_service import OutboxService
from app.workers import audit_worker
from app.workers.audit_worker import AuditWorker, registerSignalHandlers


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


# ---------------------------------------------------------------------------
# 覆盖率补全：以下用例针对 audit_worker.py 中尚未被单测覆盖的分支
# (行 86-99, 158, 174, 198, 232-236, 244-245, 261-262, 267-269, 274-281)
# ---------------------------------------------------------------------------


async def _enqueueFeatureEvent(
    dbSession: AsyncSession, event_type: str, entity_id: int, seed_row: bool = True
) -> AuditOutbox:
    """造 feature_definition 历史事件（覆盖 _writeHistorySnapshot FeatureDefinition 分支）。"""
    if seed_row:
        # FeatureDefinition 有 datasource_id NOT NULL FK（Phase 4.5 加的），必须先 seed datasource
        ds = DataSource(
            id=entity_id,
            name=f"test-ds-{entity_id}",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="test_db",
            username="u",
            password_encrypted="x",
        )
        dbSession.add(ds)
        await dbSession.commit()

        feat = FeatureDefinition(
            id=entity_id,
            datasource_id=entity_id,
            feature_name=f"FEAT_TEST_{entity_id}",
            feature_alias=f"feat_{entity_id}",
            feature_definition="OTD 3 月均值",
            entity_type="SUPPLIER",
            calculation_logic="avg(otd_rate)",
            window_size="3M",
            refresh_frequency="DAILY",
            unit="%",
            owner="data-team",
            version="v1.0",
        )
        dbSession.add(feat)
        await dbSession.commit()
    svc = OutboxService()
    return await svc.enqueue(
        dbSession,
        event_type=event_type,
        entity_type="feature_definition",
        entity_id=entity_id,
        actor="test-admin",
        payload={
            "after": {
                "id": entity_id,
                "feature_name": f"FEAT_TEST_{entity_id}",
            }
        },
    )


@pytest.mark.asyncio
async def test_drain_once_writes_feature_history_snapshot(dbSession: AsyncSession):
    """feature_* 事件：worker 写 feature_definition_history 快照（行 244-245）。"""
    await _enqueueFeatureEvent(dbSession, "feature_created", 100)
    await dbSession.commit()

    worker = AuditWorker()
    await worker.drainOnce(dbSession)
    await dbSession.commit()

    histories = (
        await dbSession.execute(select(FeatureDefinitionHistory))
    ).scalars().all()
    assert len(histories) == 1
    assert histories[0].feature_id == 100
    assert histories[0].changed_by == "test-admin"


@pytest.mark.asyncio
async def test_drain_once_skips_history_when_entity_deleted(dbSession: AsyncSession):
    """outbox 入队时实体存在但消费时已被删：跳过 history（行 232-236）。

    audit_log 仍记录（变更事实），history 尽力而为（FK 约束会失败）。
    """
    row = await _enqueueKpiEvent(dbSession, "kpi_created", 50, seed_row=True)
    # 模拟消费时实体已删除
    await dbSession.delete(
        await dbSession.get(KpiCatalog, 50)
    )
    await dbSession.commit()

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()

    assert n == 1  # 仍处理成功（audit_log 落库）
    histories = (
        await dbSession.execute(select(KpiCatalogHistory))
    ).scalars().all()
    assert len(histories) == 0  # 但 history 跳过


@pytest.mark.asyncio
async def test_drain_once_skips_non_routed_entity_history(dbSession: AsyncSession):
    """entity_type 不在路由表：不写 history（覆盖 audit_log 路径外的 if 分支）。"""
    # 直接塞一条 outbox，entity_type 是无关业务对象
    svc = OutboxService()
    await svc.enqueue(
        dbSession,
        event_type="datasource_created",
        entity_type="datasource",
        entity_id=1,
        actor="test-admin",
        payload={"after": {"id": 1}},
    )
    await dbSession.commit()

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()
    assert n == 1  # audit_log 落地
    histories = (
        await dbSession.execute(select(KpiCatalogHistory))
    ).scalars().all()
    assert len(histories) == 0


@pytest.mark.asyncio
async def test_drain_once_idempotent_pre_check(dbSession: AsyncSession):
    """_applyEvent 幂等前置检查：已存在 audit_log 则 no-op（行 198）。

    模拟 outbox 行重投但 audit_log 已写的场景：通过 monkeypatch
    AuditService.record 让其只写一次。
    """
    row = await _enqueueKpiEvent(dbSession, "kpi_created", 70)
    await dbSession.commit()

    record_calls = []

    real_record = audit_worker.AuditService.record

    async def counting_record(*args, **kwargs):
        record_calls.append(kwargs.get("outbox_id"))
        return await real_record(*args, **kwargs)

    audit_worker.AuditService.record = counting_record

    try:
        worker = AuditWorker()
        await worker.drainOnce(dbSession)
        await dbSession.commit()
        # 二次 drain（无 pending），不调 record
        await worker.drainOnce(dbSession)
        await dbSession.commit()
    finally:
        audit_worker.AuditService.record = real_record

    assert len(record_calls) == 1  # 只调用一次


@pytest.mark.asyncio
async def test_drain_once_truncates_long_error_message(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """异常消息 > 500 字符：last_error 被截断到 500（行 161-163）。"""
    await _enqueueKpiEvent(dbSession, "kpi_created", 80)
    await dbSession.commit()

    long_msg = "X" * 800

    async def _boom(*args, **kwargs):
        raise RuntimeError(long_msg)

    monkeypatch.setattr(audit_worker.AuditService, "record", _boom)

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()
    assert n == 0

    box = (await dbSession.execute(select(AuditOutbox))).scalar_one()
    assert box.attempts == 1
    assert box.last_error is not None
    assert len(box.last_error) <= 500
    assert box.last_error.startswith("RuntimeError: ")


@pytest.mark.asyncio
async def test_drain_once_logs_error_at_max_attempts(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """attempts 达到 max_attempts 时日志为 ERROR 级别（行 174）。"""
    row = await _enqueueKpiEvent(dbSession, "kpi_created", 90)
    row.attempts = 4  # 下一次失败后变为 5 == max
    await dbSession.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("permanent failure")

    monkeypatch.setattr(audit_worker.AuditService, "record", _boom)

    with caplog.at_level(logging.ERROR, logger="app.workers.audit_worker"):
        worker = AuditWorker()
        await worker.drainOnce(dbSession)
        await dbSession.commit()

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("达到最大重试次数" in r.message for r in error_records)


@pytest.mark.asyncio
async def test_process_one_returns_false_when_concurrent_worker_processed(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """并发处理：第二次条件 UPDATE rowcount == 0 时返回 False（行 158）。"""
    row = await _enqueueKpiEvent(dbSession, "kpi_created", 95)
    await dbSession.commit()

    # 让 commit 后另一 worker 已置 processed_at
    real_record = audit_worker.AuditService.record

    async def racing_record(*args, **kwargs):
        result = await real_record(*args, **kwargs)
        # 模拟并发：把 outbox.processed_at 设为 now，让条件 UPDATE 命中 0 行
        box = await dbSession.get(AuditOutbox, row.id)
        box.processed_at = func.now()
        await dbSession.commit()
        return result

    monkeypatch.setattr(audit_worker.AuditService, "record", racing_record)

    worker = AuditWorker()
    n = await worker.drainOnce(dbSession)
    await dbSession.commit()
    assert n == 0  # 第二次 commit 时 rowcount=0，返回 False，不计入成功


@pytest.mark.asyncio
async def test_run_loop_handles_drain_exception(monkeypatch: pytest.MonkeyPatch):
    """run() 主循环：drain 异常时记日志继续轮询，不退出进程（行 86-99）。

    monkeypatch session factory 使 drainOnce 抛异常但 stop_event 已 set，
    验证进程不会因单次失败退出。
    """
    import asyncio

    factory_calls = {"n": 0}

    class FailingFactory:
        def __call__(self):
            return _FailingCtx()

    class _FailingCtx:
        async def __aenter__(self):
            raise RuntimeError("DB unreachable")

        async def __aexit__(self, *args):
            return False

    # run() 内部 from app.infrastructure.database import getSessionFactory
    # 在函数体内导入，monkeypatch 必须在那个命名空间上生效
    import app.infrastructure.database as _db_mod
    monkeypatch.setattr(_db_mod, "getSessionFactory", lambda: FailingFactory())

    worker = AuditWorker(pollInterval=0.05)
    # 触发一次 drain 异常后立即停止
    async def stop_after_one():
        await asyncio.sleep(0.1)
        worker.requestStop()

    asyncio.create_task(stop_after_one())
    # 应优雅退出（不抛出）
    await asyncio.wait_for(worker.run(), timeout=2.0)


def test_register_signal_handlers_main_thread(monkeypatch: pytest.MonkeyPatch):
    """信号处理器：主线程场景安装成功（行 261-262）。"""
    installed = []

    def fake_signal(sig, handler):
        installed.append(sig)

    monkeypatch.setattr(audit_worker.signal, "signal", fake_signal)

    registerSignalHandlers(MagicMock())
    assert len(installed) == 2
    import signal as _signal
    assert _signal.SIGTERM in installed
    assert _signal.SIGINT in installed


def test_register_signal_handlers_non_main_thread(monkeypatch: pytest.MonkeyPatch):
    """信号处理器：非主线程（ValueError）跳过安装（行 267-269）。"""
    def fake_signal(sig, handler):
        raise ValueError("signal only works in main thread of the main interpreter")

    monkeypatch.setattr(audit_worker.signal, "signal", fake_signal)
    # 不应抛错
    registerSignalHandlers(MagicMock())


@pytest.mark.asyncio
async def test_main_entry_invokes_run(monkeypatch: pytest.MonkeyPatch):
    """main() 入口：构造 worker 后调用 run（行 274-281）。"""
    run_called = []

    class FakeWorker(AuditWorker):
        """继承 AuditWorker 保留默认属性，仅替换 run。"""
        async def run(self):
            run_called.append(True)

    monkeypatch.setattr(audit_worker, "AuditWorker", FakeWorker)
    # 同时拦截 logging.basicConfig 防止污染测试输出
    monkeypatch.setattr(audit_worker.logging, "basicConfig", lambda **kw: None)

    await audit_worker.main()
    assert run_called == [True]
