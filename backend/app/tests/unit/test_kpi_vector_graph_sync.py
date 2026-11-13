"""KPI 向量/图同步核心：文本构造、MEASURED_BY 边集合语义、best-effort 装配。

**绝不**触碰真实 Milvus / Neo4j（本地 19530 / 7687 是生产实例）：
- Milvus：monkeypatch `sync.insertEmbeddingsDual` / `sync.deleteByOntologyIdDual`
- Neo4j：monkeypatch `sync.neo4j`；`neo4j_client.setKpiMeasuredBy` 用 fake driver 记事务内 CQL
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from app.domain.models import KpiCatalog
from app.infrastructure import neo4j_client
from app.services import kpi_vector_graph_sync as sync


def _kpi(**over) -> KpiCatalog:
    base = dict(
        id=1, kpi_code="KPI_OTD", kpi_name="准时交付率",
        business_definition="按期到货的采购单占比", unit="%",
        semantic_keywords=["交付", "OTD"], status="PUBLISHED", metric_id=None,
    )
    base.update(over)
    return KpiCatalog(**base)


async def _drainLoop(times: int = 10) -> None:
    """让 create_task 真正跑完 + done_callback(discard) 被事件循环执行。"""
    for _ in range(times):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 文本构造
# ---------------------------------------------------------------------------


def test_embedding_text_full() -> None:
    text = sync.kpiEmbeddingText(_kpi(), "on-time delivery")
    assert text == "准时交付率 on-time delivery 交付 OTD 按期到货的采购单占比 %"


def test_embedding_text_skips_empty_segments() -> None:
    text = sync.kpiEmbeddingText(
        _kpi(semantic_keywords=None, business_definition=None, unit=None), None
    )
    assert text == "准时交付率"


def test_embedding_text_is_stable() -> None:
    assert sync.kpiEmbeddingText(_kpi(), None) == sync.kpiEmbeddingText(_kpi(), None)


def test_constants() -> None:
    assert sync.KPI_BUSINESS_OBJECT == "KPI"
    assert sync.KPI_NEO4J_LABEL == "KpiCatalog"


# ---------------------------------------------------------------------------
# best-effort 装配
# ---------------------------------------------------------------------------


async def test_spawn_runs_task_and_releases_reference() -> None:
    """spawn 出的后台任务能被 _PENDING_KPI_SYNC_TASKS 持握（防 GC 丢失），完成后回收。"""
    calls: list[tuple] = []

    async def _fakeNow(session, kpiId):
        calls.append(("vector", kpiId))

    sync._spawn(_fakeNow(None, 1), kpiId=1, operation="vector")
    await _drainLoop()

    assert ("vector", 1) in calls
    assert not sync._PENDING_KPI_SYNC_TASKS  # done 后回收


async def test_spawn_isolates_failures(caplog: pytest.LogCaptureFixture) -> None:
    """D4 隔离：抛异常的协程不得从后台任务冒出去，且日志要能定位到哪条 KPI 的哪一步。"""

    async def _boom() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        sync._spawn(_boom(), kpiId=42, operation="delete-vector")
        await _drainLoop()

    assert not sync._PENDING_KPI_SYNC_TASKS
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "失败必须留下 warning（不能静默）"
    assert any("42" in m and "delete-vector" in m for m in warnings)


# ---------------------------------------------------------------------------
# neo4j_client.setKpiMeasuredBy：MEASURED_BY 边集合必须被**替换**而非追加
# ---------------------------------------------------------------------------


class _FakeResult:
    """最小 fake result：支持 .single() / 迭代。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def single(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeTx:
    """fake 事务对象：记录 (cql, params)，并按 CQL 形状返回可控结果行。"""

    def __init__(
        self,
        statements: list,
        *,
        kpiExists: bool = True,
        metricPresent: bool = True,
    ) -> None:
        self._statements = statements
        self._kpiExists = kpiExists
        self._metricPresent = metricPresent

    def run(self, cql: str, **params):
        self._statements.append((cql, params))
        if "count(k) AS cnt" in cql:
            return _FakeResult([{"cnt": 1 if self._kpiExists else 0}])
        if "RETURN m.unified_id AS metricUid" in cql:
            if self._metricPresent:
                return _FakeResult([{"metricUid": params["metricUid"]}])
            return _FakeResult([])
        return _FakeResult([])


class _FakeNeo4jSession:
    """fake session：**只有** execute_write（没有 run）⇒ 事务外语句会直接炸。"""

    def __init__(
        self,
        recorder: list,
        *,
        kpiExists: bool = True,
        metricPresent: bool = True,
    ) -> None:
        self._recorder = recorder
        self._kpiExists = kpiExists
        self._metricPresent = metricPresent

    def execute_write(self, txFunction, *args, **kwargs):
        statements: list[tuple[str, dict]] = []
        tx = _FakeTx(
            statements,
            kpiExists=self._kpiExists,
            metricPresent=self._metricPresent,
        )
        result = txFunction(tx, *args, **kwargs)
        self._recorder.append(statements)
        return result

    def run(self, *args, **kwargs):
        raise AssertionError("setKpiMeasuredBy 必须在显式事务内执行（不得用 session.run）")

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


class _FakeDriver:
    def __init__(
        self,
        recorder: list,
        *,
        kpiExists: bool = True,
        metricPresent: bool = True,
    ) -> None:
        self._recorder = recorder
        self._kpiExists = kpiExists
        self._metricPresent = metricPresent

    def session(self) -> _FakeNeo4jSession:
        return _FakeNeo4jSession(
            self._recorder,
            kpiExists=self._kpiExists,
            metricPresent=self._metricPresent,
        )


def _patchDriver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kpiExists: bool = True,
    metricPresent: bool = True,
) -> list:
    """把 getDriver 换成 fake driver，返回「每个事务一条语句列表」的 recorder。"""
    recorder: list = []
    monkeypatch.setattr(
        neo4j_client,
        "getDriver",
        lambda: _FakeDriver(
            recorder, kpiExists=kpiExists, metricPresent=metricPresent
        ),
    )
    return recorder


def test_set_measured_by_replaces_edge_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """先删旧边、再建新边 ⇒ 边集合恰好一条。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    assert len(recorder) == 1
    statements = recorder[0]
    cqls = [cql for cql, _ in statements]
    deleteIdx = next(i for i, c in enumerate(cqls) if "DELETE r" in c)
    linkIdx = next(i for i, c in enumerate(cqls) if "MERGE (k)-[:MEASURED_BY]->(m)" in c)
    assert deleteIdx < linkIdx, "必须先删后建"

    deleteCql, deleteParams = statements[deleteIdx]
    assert ":MEASURED_BY" in deleteCql
    assert deleteParams == {"kpiUid": "obj:KPI:1"}

    linkCql, linkParams = statements[linkIdx]
    assert "(m:Metric {unified_id: $metricUid})" in linkCql
    assert linkParams == {"kpiUid": "obj:KPI:1", "metricUid": "obj:METRIC:5"}


def test_set_measured_by_single_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """删边与建边必须在**同一个事务**里：中途失败不得留下「删了没建」的半应用状态。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    assert len(recorder) == 1, "删边+建边必须落在同一个事务里"
    cqls = [cql for cql, _ in recorder[0]]
    assert any("DELETE r" in c for c in cqls)
    assert any("MERGE (k)-[:MEASURED_BY]->(m)" in c for c in cqls)


def test_set_measured_by_none_only_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    """metricUid=None ⇒ 只删旧边、不建新边（KPI 已解绑指标）。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", None)

    assert len(recorder) == 1
    cqls = [cql for cql, _ in recorder[0]]
    assert any("DELETE r" in c for c in cqls)
    assert not any("MERGE" in c for c in cqls)
    assert not any("metricUid" in c for c in cqls), "None 分支不得发建边语句"


def test_set_measured_by_empty_string_still_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metricUid='' 是「建边到空串 id」，不是「不建边」⇒ 用 is not None 判定。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", "")

    cqls = [cql for cql, _ in recorder[0]]
    assert any("MERGE (k)-[:MEASURED_BY]->(m)" in c for c in cqls)


def test_set_measured_by_never_creates_metric_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向守卫：不得 CREATE metric 节点（两端都 MATCH ⇒ 指标未入图时不凭空造点）。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    assert recorder, "setKpiMeasuredBy 必须真的发出语句"
    for statements in recorder:
        for cql, _params in statements:
            assert "CREATE" not in cql
    linkCql = next(
        cql for cql, _ in recorder[0] if "MERGE (k)-[:MEASURED_BY]->(m)" in cql
    )
    # metric 端是 MATCH（而非 MERGE/CREATE）⇒ 指标节点不在图里时不凭空造点
    assert "(m:Metric {unified_id: $metricUid})" in linkCql


def test_set_measured_by_warns_when_metric_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """KPI 在、目标 metric 不在图里 ⇒ 必须 warning（KPI uid + metric uid 都带上）。"""
    _patchDriver(monkeypatch, metricPresent=False)
    with caplog.at_level(logging.WARNING):
        neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "目标指标未入图必须可观测"
    assert any("obj:KPI:1" in m and "obj:METRIC:5" in m for m in warnings)


def test_set_measured_by_silent_when_kpi_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """KPI 节点本身不存在 ⇒ 静默无操作（文档约定），不产生 warning。"""
    _patchDriver(monkeypatch, kpiExists=False)
    with caplog.at_level(logging.WARNING):
        neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings


# ---------------------------------------------------------------------------
# syncKpiGraphNow：MEASURED_BY 边集合必须与当前 metric_id 精确一致
# ---------------------------------------------------------------------------


class _FakeNeo4j:
    """记录图写入调用（fake double，不连真 Neo4j）。"""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.measuredBy: list[tuple[str, str | None]] = []
        self.deleted: list[tuple[str, str]] = []

    def upsertKpiCatalogNode(self, **kwargs) -> None:
        self.upserts.append(kwargs)

    def setKpiMeasuredBy(self, kpiUid: str, metricUid: str | None) -> None:
        self.measuredBy.append((kpiUid, metricUid))

    def deleteNode(self, label: str, unified_id: str) -> None:
        self.deleted.append((label, unified_id))


class _FakeIdMappingService:
    """fake IdMappingService：按 (business_object, external_id) 解析 unified_id。"""

    def __init__(
        self,
        metricUids: dict[str, str] | None = None,
        *,
        kpiUid: str | None = "obj:KPI:{id}",
    ) -> None:
        self._metricUids = metricUids or {}
        self._kpiUid = kpiUid
        self.calls: list[tuple[str, str]] = []

    async def resolveByExternal(self, session, business_object: str, external_id: str):
        self.calls.append((business_object, external_id))
        if business_object == sync.KPI_BUSINESS_OBJECT:
            if self._kpiUid is None:
                return None
            return SimpleNamespace(
                unified_id=self._kpiUid.format(id=external_id),
                pg_table="kpi_catalog",
            )
        if business_object == "METRIC":
            uid = self._metricUids.get(external_id)
            if uid is not None:
                return SimpleNamespace(unified_id=uid)
        return None


class _FakeGraphSession:
    """只满足 `session.get(KpiCatalog, id)` 的最小 session。"""

    def __init__(self, kpi: KpiCatalog | None) -> None:
        self._kpi = kpi

    async def get(self, model, pk):
        if self._kpi is not None and pk == self._kpi.id:
            return self._kpi
        return None


def _patchGraphDeps(
    monkeypatch: pytest.MonkeyPatch,
    metricUids=None,
    kpiUid: str | None = "obj:KPI:{id}",
):
    """kpiUid=None ⇒ 假装 KPI 没有 id_mapping 行（走「未注册」分支）。"""
    fakeNeo4j = _FakeNeo4j()
    fakeIdMapping = _FakeIdMappingService(metricUids, kpiUid=kpiUid)
    monkeypatch.setattr(sync, "neo4j", fakeNeo4j)
    monkeypatch.setattr(sync, "IdMappingService", lambda: fakeIdMapping)
    return fakeNeo4j, fakeIdMapping


async def test_graph_sync_sets_measured_by_to_current_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KPI 挂 metric A ⇒ MEASURED_BY 边集合 = {A}。"""
    fakeNeo4j, _ = _patchGraphDeps(monkeypatch, {"7": "obj:METRIC:7"})
    kpi = _kpi(metric_id=7)

    uid = await sync.syncKpiGraphNow(_FakeGraphSession(kpi), kpi.id)

    assert uid == "obj:KPI:1"
    assert fakeNeo4j.upserts[0]["code"] == "KPI_OTD"
    assert fakeNeo4j.upserts[0]["status"] == "PUBLISHED"
    assert fakeNeo4j.measuredBy == [("obj:KPI:1", "obj:METRIC:7")]


async def test_graph_sync_clears_measured_by_when_metric_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric_id 为空 ⇒ 边集合置零（而不是留着上一条旧边）。"""
    fakeNeo4j, _ = _patchGraphDeps(monkeypatch)
    kpi = _kpi(metric_id=None)

    await sync.syncKpiGraphNow(_FakeGraphSession(kpi), kpi.id)

    assert fakeNeo4j.measuredBy == [("obj:KPI:1", None)]


async def test_graph_sync_clears_measured_by_when_metric_unmapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """metric_id 指向的指标尚未入 id_mapping ⇒ 同样置零边。"""
    fakeNeo4j, _ = _patchGraphDeps(monkeypatch, {})  # 指标无映射
    kpi = _kpi(metric_id=99)

    await sync.syncKpiGraphNow(_FakeGraphSession(kpi), kpi.id)

    assert fakeNeo4j.measuredBy == [("obj:KPI:1", None)]


# ---------------------------------------------------------------------------
# 删除路径
# ---------------------------------------------------------------------------


async def test_delete_kpi_vector_now_deletes_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        sync, "deleteByOntologyIdDual", lambda oid, typ: calls.append((oid, typ))
    )

    await sync.deleteKpiVectorNow(7)

    assert calls == [(7, "kpi")]


async def test_delete_kpi_graph_now_with_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakeNeo4j, _ = _patchGraphDeps(monkeypatch)

    await sync.deleteKpiGraphNow(_FakeGraphSession(None), 1)

    assert fakeNeo4j.deleted == [("KpiCatalog", "obj:KPI:1")]


async def test_delete_kpi_graph_now_without_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 id_mapping 行 ⇒ 从未入图 ⇒ 无事可做（不得调 deleteNode）。"""
    fakeNeo4j, _ = _patchGraphDeps(monkeypatch, kpiUid=None)

    await sync.deleteKpiGraphNow(_FakeGraphSession(None), 1)

    assert fakeNeo4j.deleted == []


# ---------------------------------------------------------------------------
# 四个 best-effort 入口（Task 4 直接调用它们）
# ---------------------------------------------------------------------------


class _FakeSessionCtx:
    """async with 上下文：产出给定的 session 对象。"""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc) -> bool:
        return False


async def test_sync_kpi_graph_best_effort_uses_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    sentinel = object()

    async def _fakeGraph(session, kpiId):
        calls.append((session, kpiId))

    monkeypatch.setattr(sync, "syncKpiGraphNow", _fakeGraph)
    monkeypatch.setattr(sync, "_ownSession", lambda: _FakeSessionCtx(sentinel))

    await sync.syncKpiGraphBestEffort(5)
    await _drainLoop()

    assert calls == [(sentinel, 5)]


async def test_sync_kpi_vector_best_effort_uses_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    sentinel = object()

    async def _fakeVector(session, kpiId):
        calls.append((session, kpiId))

    monkeypatch.setattr(sync, "syncKpiVectorNow", _fakeVector)
    monkeypatch.setattr(sync, "_ownSession", lambda: _FakeSessionCtx(sentinel))

    await sync.syncKpiVectorBestEffort(6)
    await _drainLoop()

    assert calls == [(sentinel, 6)]


async def test_delete_kpi_vector_best_effort_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        sync, "deleteByOntologyIdDual", lambda oid, typ: calls.append((oid, typ))
    )

    await sync.deleteKpiVectorBestEffort(8)
    await _drainLoop()

    assert calls == [(8, "kpi")]


async def test_delete_kpi_graph_best_effort_uses_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    sentinel = object()

    async def _fakeDeleteGraph(session, kpiId):
        calls.append((session, kpiId))

    monkeypatch.setattr(sync, "deleteKpiGraphNow", _fakeDeleteGraph)
    monkeypatch.setattr(sync, "_ownSession", lambda: _FakeSessionCtx(sentinel))

    await sync.deleteKpiGraphBestEffort(9)
    await _drainLoop()

    assert calls == [(sentinel, 9)]


# ---------------------------------------------------------------------------
# Milvus 阻塞调用不得跑在事件循环线程上；且必须串行化（Finding B）
# ---------------------------------------------------------------------------


class _FakeDbResult:
    """SQLAlchemy 结果的最小 fake：只支持 .first()（行是 (model, alias) 元组）。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDbSession:
    """满足 `_loadKpiWithMetricAlias` 的最小 DB session（同时是 async with 上下文）。"""

    def __init__(self, kpi: KpiCatalog) -> None:
        self._kpi = kpi

    async def execute(self, stmt):
        return _FakeDbResult([(self._kpi, None)])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeEmbedding:
    async def generateEmbedding(self, text: str) -> list[float]:
        return [0.0] * 4


async def test_milvus_write_runs_off_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pymilvus 是同步 SDK（insert/delete 后要 flush）⇒ 不得跑在事件循环线程上。"""
    calls: list[tuple] = []
    loopThread = threading.get_ident()

    def _fakeDelete(oid, typ):
        calls.append((oid, typ, threading.get_ident()))

    monkeypatch.setattr(sync, "deleteByOntologyIdDual", _fakeDelete)
    monkeypatch.setattr(sync, "insertEmbeddingsDual", lambda records: None)
    monkeypatch.setattr(sync, "EmbeddingService", _FakeEmbedding)
    monkeypatch.setattr(sync, "_ownSession", lambda: _FakeDbSession(_kpi()))

    await sync._runVectorOwnSession(1)

    assert calls, "必须真的调了 Milvus 删除"
    assert calls[0][2] != loopThread, "阻塞式 Milvus 调用不得跑在事件循环线程上"


async def test_milvus_writes_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发 KPI 同步不得并发 flush 同一集合 ⇒ 一次 KPI 的删+插是一个临界区。"""
    state = {"active": 0, "maxActive": 0}
    guard = threading.Lock()

    def _fakeDelete(oid, typ):
        with guard:
            state["active"] += 1
            state["maxActive"] = max(state["maxActive"], state["active"])
        time.sleep(0.05)
        with guard:
            state["active"] -= 1

    monkeypatch.setattr(sync, "deleteByOntologyIdDual", _fakeDelete)
    monkeypatch.setattr(sync, "insertEmbeddingsDual", lambda records: None)
    monkeypatch.setattr(sync, "EmbeddingService", _FakeEmbedding)
    monkeypatch.setattr(sync, "_ownSession", lambda: _FakeDbSession(_kpi()))

    await asyncio.gather(
        sync._runVectorOwnSession(1), sync._runVectorOwnSession(2)
    )

    assert state["maxActive"] == 1, "Milvus 写入段必须串行（并发 flush 会拖垮 Milvus）"
