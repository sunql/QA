"""KPI 图/向量同步集成：真 PG（5434 qa_metadata_test），neo4j/milvus 用 fake double。

本地 19530/7687 是生产实例，禁连；graph/milvus 调用全部落到内存 recorder。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete as saDelete

from app.domain.models import IdMapping, KpiCatalog
from app.services import kpi_vector_graph_sync as sync
from app.services.id_mapping_service import IdMappingService

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seedKpi(dbSession):
    """一条 PUBLISHED 的 KPI（真 PG）。"""
    kpi = KpiCatalog(
        kpi_code="KPI_SYNC_TEST", kpi_name="同步测试", status="PUBLISHED",
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


@pytest.fixture()
def fakeStores(monkeypatch):
    """neo4j / milvus 的内存 recorder（**不连真实例**）。"""
    recorder: dict[str, list] = {
        "upsert": [], "measuredBy": [], "link": [], "delete": [],
        "insert": [], "milvusDelete": [],
    }

    class _FakeNeo4j:
        def upsertKpiCatalogNode(self, **kw):
            recorder["upsert"].append(kw)

        def setKpiMeasuredBy(self, kpiUid, metricUid):
            recorder["measuredBy"].append((kpiUid, metricUid))

        def linkKpiMeasuredBy(self, kpiUid, metricUid):
            # 只加不删的语义（对账路径用）；钩子路径**不该**走这里
            recorder["link"].append((kpiUid, metricUid))

        def deleteNode(self, label, unified_id):
            recorder["delete"].append((label, unified_id))

    class _FakeEmbedding:
        async def generateEmbedding(self, text):
            return [0.0] * 4

    monkeypatch.setattr(sync, "neo4j", _FakeNeo4j())
    monkeypatch.setattr(sync, "EmbeddingService", _FakeEmbedding)
    monkeypatch.setattr(
        sync, "insertEmbeddingsDual", lambda records: recorder["insert"].extend(records)
    )
    monkeypatch.setattr(
        sync, "deleteByOntologyIdDual",
        lambda oid, typ: recorder["milvusDelete"].append((oid, typ)),
    )
    return recorder


async def cleanupKpiMapping(dbSession, kpiId: int) -> None:
    """id_mapping business_object='KPI' 测试残留必须清掉（共享真 PG 库卫生）。"""
    await dbSession.execute(
        saDelete(IdMapping).where(IdMapping.business_object == "KPI",
                                  IdMapping.pg_id == str(kpiId))
    )
    await dbSession.commit()


async def test_graph_sync_registers_mapping_and_upserts(
    seedKpi, fakeStores, dbSession
):
    kpi = seedKpi
    try:
        uid = await sync.syncKpiGraphNow(dbSession, kpi.id)
        assert uid == f"obj:KPI:{kpi.id}"
        row = await IdMappingService().resolveByExternal(
            dbSession, "KPI", str(kpi.id)
        )
        assert row is not None and row.pg_table == "kpi_catalog"
        assert fakeStores["upsert"][0]["code"] == "KPI_SYNC_TEST"
        assert fakeStores["upsert"][0]["status"] == "PUBLISHED"
    finally:
        await cleanupKpiMapping(dbSession, kpi.id)


async def test_graph_sync_clears_measured_by_without_metric(
    seedKpi, fakeStores, dbSession
):
    """无 metric_id ⇒ MEASURED_BY 边集合置零；且**不得**走只加不删的 link 路径。"""
    kpi = seedKpi
    try:
        await sync.syncKpiGraphNow(dbSession, kpi.id)
        assert fakeStores["measuredBy"] == [(f"obj:KPI:{kpi.id}", None)]
        assert fakeStores["link"] == []
    finally:
        await cleanupKpiMapping(dbSession, kpi.id)


async def test_vector_sync_published_then_draft(
    seedKpi, fakeStores, dbSession
):
    kpi = seedKpi
    try:
        await sync.syncKpiVectorNow(dbSession, kpi.id)
        # PUBLISHED：删一次 + 插一次（external_id=kpi_code）
        assert fakeStores["milvusDelete"] == [(kpi.id, "kpi")]
        assert len(fakeStores["insert"]) == 1
        assert fakeStores["insert"][0]["external_id"] == "KPI_SYNC_TEST"
        assert fakeStores["insert"][0]["type"] == "kpi"
        assert fakeStores["insert"][0]["ontology_id"] == kpi.id
        # 转 DRAFT：再跑一次 ⇒ 删两次、不再插入（D3）
        kpi.status = "DRAFT"
        await dbSession.commit()
        await sync.syncKpiVectorNow(dbSession, kpi.id)
        assert fakeStores["milvusDelete"] == [(kpi.id, "kpi"), (kpi.id, "kpi")]
        assert len(fakeStores["insert"]) == 1
    finally:
        await cleanupKpiMapping(dbSession, kpi.id)
