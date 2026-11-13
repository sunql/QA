"""syncMissingGraph 的 Metric/KPI 图节点对账段（真 PG + fake neo4j）。

覆盖 Task 6：
1. reconcileKpiGraph —— KPI 图节点补齐 + MEASURED_BY 边（幂等）；
2. syncMissingGraph —— Metric 图节点 + KPI 图节点一并对账。

外部依赖边界：Neo4j **全程假替身**（禁连真 7687 / 生产图谱），数据层走真实
PostgreSQL（Harness/rules/测试规范.md）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete as saDelete

from app.domain.models import IdMapping, KpiCatalog, OntologyMetric
from app.domain.schemas import IdMappingCreate
from app.services import kpi_vector_graph_sync as sync
from app.services.id_mapping_service import IdMappingService
from app.services.ontology_service import OntologyService

pytestmark = pytest.mark.asyncio

_METRIC_NAME = "同步率_RECON_TEST"
_KPI_CODE = "KPI_RECON_TEST"


@pytest.fixture()
def neo4jRecorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """fake neo4j 全套读/写（禁连真 7687）。"""
    rec: dict[str, list] = {
        "kpiUpsert": [],
        "metricUpsert": [],
        "measuredBy": [],
        "derivedFrom": [],
        "kpiIds": set(),
        "metricIds": set(),
    }

    class _FakeNeo4j:
        @staticmethod
        def getKpiCatalogIds() -> set[str]:
            return set(rec["kpiIds"])

        @staticmethod
        def getMetricIds() -> set[str]:
            return set(rec["metricIds"])

        @staticmethod
        def getClassIds() -> set[str]:
            return set()

        @staticmethod
        def getPropertyIds() -> set[str]:
            return set()

        @staticmethod
        def getJoinPairs() -> set[tuple[str, str]]:
            return set()

        @staticmethod
        def getRelationTriples() -> set[tuple[str, str, str]]:
            return set()

        @staticmethod
        def upsertKpiCatalogNode(**kw) -> None:
            rec["kpiUpsert"].append(kw)
            rec["kpiIds"].add(kw["unified_id"])

        @staticmethod
        def upsertMetricNode(**kw) -> None:
            rec["metricUpsert"].append(kw)
            rec["metricIds"].add(kw["unified_id"])

        @staticmethod
        def linkKpiMeasuredBy(kpiUid, metricUid) -> None:
            rec["measuredBy"].append((kpiUid, metricUid))

        @staticmethod
        def linkMetricDerivedFrom(metricUid, classUid) -> None:
            rec["derivedFrom"].append((metricUid, classUid))

        # syncMissingGraph 还会触达的既有写函数：record 一律 no-op
        @staticmethod
        def upsertClassNode(**kw) -> None:
            pass

        @staticmethod
        def upsertPropertyNode(**kw) -> None:
            pass

        @staticmethod
        def reconcileClassSubclassOf(*a, **k) -> None:
            pass

        @staticmethod
        def linkClassHasProperty(*a, **k) -> None:
            pass

        @staticmethod
        def linkPropertyReferences(*a, **k) -> None:
            pass

        @staticmethod
        def linkClassJoin(*a, **k) -> None:
            pass

        @staticmethod
        def linkClassRelation(*a, **k) -> None:
            pass

    monkeypatch.setattr(sync, "neo4j", _FakeNeo4j)
    from app.services import ontology_service as osvc

    monkeypatch.setattr(osvc, "neo4j", _FakeNeo4j)
    return rec


@pytest_asyncio.fixture
async def seedGraphRows(dbSession):
    """真 PG 种子（async fixture）。"""
    metric = OntologyMetric(
        metric_name=_METRIC_NAME,
        formula="SUM(a)/SUM(b)",
        agg_function="ratio",
    )
    dbSession.add(metric)
    await dbSession.flush()
    kpi = KpiCatalog(
        kpi_code=_KPI_CODE,
        kpi_name="对账测试",
        status="PUBLISHED",
        metric_id=metric.id,
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(metric)
    await dbSession.refresh(kpi)
    return metric, kpi


async def _cleanup(session, metricId: int, kpiId: int) -> None:
    """按 seed 出的真实 id / code 精确清理三张表（禁止全表删）。"""
    await session.execute(
        saDelete(IdMapping).where(
            IdMapping.business_object == "KPI",
            IdMapping.pg_id == str(kpiId),
        )
    )
    await session.execute(
        saDelete(IdMapping).where(
            IdMapping.business_object == "METRIC",
            IdMapping.pg_id == str(metricId),
        )
    )
    await session.execute(saDelete(KpiCatalog).where(KpiCatalog.id == kpiId))
    await session.execute(saDelete(OntologyMetric).where(OntologyMetric.id == metricId))
    await session.commit()


async def test_reconcile_kpi_graph_idempotent(seedGraphRows, neo4jRecorder, dbSession):
    metric, kpi = seedGraphRows
    try:
        # 预置 METRIC id_mapping（模拟既有 metric 已注册，KPI 未注册）
        await IdMappingService().register(
            dbSession,
            IdMappingCreate(business_object="METRIC", external_id=str(metric.id)),
            pg_table="ontology_metric",
            pg_id=str(metric.id),
        )
        first = await sync.reconcileKpiGraph(dbSession)
        assert first["missingKpiCount"] == 1
        assert first["syncedKpiCount"] == 1
        assert first["failedCount"] == 0
        assert neo4jRecorder["kpiUpsert"][0]["unified_id"] == f"obj:KPI:{kpi.id}"
        # MEASURED_BY：metric_id 挂靠 + METRIC 映射已注册 ⇒ 建边
        assert neo4jRecorder["measuredBy"] == [
            (f"obj:KPI:{kpi.id}", f"obj:METRIC:{metric.id}")
        ]
        second = await sync.reconcileKpiGraph(dbSession)
        assert second["syncedKpiCount"] == 0  # 幂等：节点已在，不再 upsert
    finally:
        await _cleanup(dbSession, metric.id, kpi.id)


async def test_reconcile_kpi_graph_skips_edge_without_metric_mapping(
    seedGraphRows, neo4jRecorder, dbSession
):
    """挂靠指标未在 id_mapping 注册 ⇒ 只建节点，**不建** MEASURED_BY 边。

    反向守卫：边的另一端（Metric 节点）必须先存在，否则图里会出现指向空气的边。
    """
    metric, kpi = seedGraphRows
    orphan = KpiCatalog(
        kpi_code="KPI_RECON_NO_METRIC_MAP",
        kpi_name="无边 KPI",
        status="DRAFT",
        metric_id=metric.id,
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(orphan)
    await dbSession.commit()
    await dbSession.refresh(orphan)
    try:
        result = await sync.reconcileKpiGraph(dbSession)
        assert result["totalKpis"] == 2
        assert result["syncedKpiCount"] == 2
        assert result["failedCount"] == 0
        # 两条 KPI 挂靠的指标都未注册 id_mapping ⇒ 一条边都不建
        assert neo4jRecorder["measuredBy"] == []
    finally:
        await dbSession.execute(
            saDelete(IdMapping).where(
                IdMapping.business_object == "KPI",
                IdMapping.pg_id == str(orphan.id),
            )
        )
        await dbSession.execute(
            saDelete(KpiCatalog).where(KpiCatalog.id == orphan.id)
        )
        await dbSession.commit()
        await _cleanup(dbSession, metric.id, kpi.id)


async def test_sync_missing_graph_covers_metric_and_kpi(
    seedGraphRows, neo4jRecorder, dbSession
):
    metric, kpi = seedGraphRows
    try:
        summary = await OntologyService().syncMissingGraph(dbSession)
        # 库内只有本用例 seed 的 1 指标 + 1 KPI（每测试 TRUNCATE 全库）⇒ 精确值，
        # 用 >= 会放过「重复计数 / 口径漂移」这类真缺陷。
        assert summary["totalMetrics"] == 1
        assert summary["syncedMetricCount"] == 1
        assert summary["missingMetricCount"] == 1  # 与既有 missingClassCount 同语义
        assert summary["totalKpis"] == 1
        assert summary["syncedKpiCount"] == 1
        assert summary["missingKpiCount"] == 1
        assert summary["failedCount"] == 0
        assert neo4jRecorder["metricUpsert"] == [
            {
                "unified_id": f"obj:METRIC:{metric.id}",
                "name": _METRIC_NAME,
                "alias": None,
                "formula": "SUM(a)/SUM(b)",
                "aggFunction": "ratio",
            }
        ]
        assert neo4jRecorder["kpiUpsert"][0]["unified_id"] == f"obj:KPI:{kpi.id}"
        # 指标段先跑 ⇒ METRIC 映射已注册 ⇒ KPI 段的 MEASURED_BY 边建得出来
        assert neo4jRecorder["measuredBy"] == [
            (f"obj:KPI:{kpi.id}", f"obj:METRIC:{metric.id}")
        ]
    finally:
        await _cleanup(dbSession, metric.id, kpi.id)
