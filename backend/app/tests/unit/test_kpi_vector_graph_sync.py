"""KPI 向量/图同步核心：文本构造、MEASURED_BY 边集合语义、best-effort 装配。

**绝不**触碰真实 Milvus / Neo4j（本地 19530 / 7687 是生产实例）：
- Milvus：monkeypatch `sync.insertEmbeddingsDual` / `sync.deleteByOntologyIdDual`
- Neo4j：monkeypatch `sync.neo4j`；`neo4j_client.setKpiMeasuredBy` 用 fake driver 记 CQL
"""

from __future__ import annotations

import asyncio
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

    sync._spawn(_fakeNow(None, 1))
    # 让 create_task 真正跑完 + done_callback(discard) 被事件循环执行
    for _ in range(10):
        await asyncio.sleep(0)

    assert ("vector", 1) in calls
    assert not sync._PENDING_KPI_SYNC_TASKS  # done 后回收


# ---------------------------------------------------------------------------
# neo4j_client.setKpiMeasuredBy：MEASURED_BY 边集合必须被**替换**而非追加
# ---------------------------------------------------------------------------


class _FakeSession:
    """最小 fake session：记录 (cql, params)。"""

    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    def run(self, cql: str, **params):
        self._recorder.append((cql, params))
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


class _FakeDriver:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    def session(self) -> _FakeSession:
        return _FakeSession(self._recorder)


def _patchDriver(monkeypatch: pytest.MonkeyPatch) -> list:
    recorder: list = []
    monkeypatch.setattr(
        neo4j_client, "getDriver", lambda: _FakeDriver(recorder)
    )
    return recorder


def test_set_measured_by_replaces_edge_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """先删旧边、再建新边（两条语句，同一 session）⇒ 边集合恰好一条。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    assert len(recorder) == 2
    deleteCql, deleteParams = recorder[0]
    linkCql, linkParams = recorder[1]
    assert "DELETE r" in deleteCql
    assert ":MEASURED_BY" in deleteCql
    assert deleteParams == {"kpiUid": "obj:KPI:1"}
    assert "MERGE (k)-[:MEASURED_BY]->(m)" in linkCql
    assert "(m:Metric {unified_id: $metricUid})" in linkCql
    assert linkParams == {"kpiUid": "obj:KPI:1", "metricUid": "obj:METRIC:5"}


def test_set_measured_by_none_only_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    """metricUid=None ⇒ 只删旧边、不建新边（KPI 已解绑指标）。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", None)

    assert len(recorder) == 1
    cql, params = recorder[0]
    assert "DELETE r" in cql
    assert ":MEASURED_BY" in cql
    assert "MERGE" not in cql
    assert params == {"kpiUid": "obj:KPI:1"}


def test_set_measured_by_never_creates_metric_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向守卫：不得 CREATE metric 节点（两端都 MATCH ⇒ 指标未入图时不凭空造点）。"""
    recorder = _patchDriver(monkeypatch)
    neo4j_client.setKpiMeasuredBy("obj:KPI:1", "obj:METRIC:5")

    assert recorder, "setKpiMeasuredBy 必须真的发出语句"
    for cql, _params in recorder:
        assert "CREATE" not in cql
    linkCql = recorder[1][0]
    assert "MATCH (k:KpiCatalog {unified_id: $kpiUid})" in linkCql
    # metric 端是 MATCH（而非 MERGE/CREATE）⇒ 指标节点不在图里时不凭空造点
    assert "(m:Metric {unified_id: $metricUid})" in linkCql


# ---------------------------------------------------------------------------
# syncKpiGraphNow：MEASURED_BY 边集合必须与当前 metric_id 精确一致
# ---------------------------------------------------------------------------


class _FakeNeo4j:
    """记录图写入调用（fake double，不连真 Neo4j）。"""

    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.measuredBy: list[tuple[str, str | None]] = []

    def upsertKpiCatalogNode(self, **kwargs) -> None:
        self.upserts.append(kwargs)

    def setKpiMeasuredBy(self, kpiUid: str, metricUid: str | None) -> None:
        self.measuredBy.append((kpiUid, metricUid))


class _FakeIdMappingService:
    """fake IdMappingService：按 (business_object, external_id) 解析 unified_id。"""

    def __init__(self, metricUids: dict[str, str] | None = None) -> None:
        self._metricUids = metricUids or {}
        self.calls: list[tuple[str, str]] = []

    async def resolveByExternal(self, session, business_object: str, external_id: str):
        self.calls.append((business_object, external_id))
        if business_object == sync.KPI_BUSINESS_OBJECT:
            return SimpleNamespace(
                unified_id=f"obj:KPI:{external_id}", pg_table="kpi_catalog"
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


def _patchGraphDeps(monkeypatch: pytest.MonkeyPatch, metricUids=None):
    fakeNeo4j = _FakeNeo4j()
    fakeIdMapping = _FakeIdMappingService(metricUids)
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
