"""B5：/metric 自动创建 DRAFT KPI 治理户头 + grain 粒度弱校验。

真实 PG + 完整 HTTP 链路（Harness 测试规范）。覆盖：
1. chat `/metric 指标名 = 公式` → ontology_metric + 幂等 DRAFT KpiCatalog（metric_id 挂靠）
2. ensureDraftForMetric 幂等：同 metric 重复调用只产生一行
3. createKpi 时 grain 未覆盖挂靠 metric 的 dimension_defaults 键 → warning 但不阻断
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.models import DataSource, KpiCatalog, OntologyMetric
from app.domain.schemas import KpiCatalogCreate
from app.services.kpi_catalog_service import KpiCatalogService

pytestmark = pytest.mark.asyncio

AUTH_HEADERS = {"X-User-Id": "metric-draft-tester", "X-User-Tenant": "default"}
_DS_ID = 9701


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession, dsId: int = _DS_ID) -> None:
    dbSession.add(
        DataSource(
            id=dsId,
            name=f"ds-metric-draft-{dsId}",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    await dbSession.commit()


def _chatPayload(question: str) -> dict:
    return {"sessionId": "sess-metric-draft", "question": question, "datasourceId": _DS_ID}


async def _getMetric(session: AsyncSession, name: str) -> OntologyMetric | None:
    row = await session.execute(
        select(OntologyMetric).where(OntologyMetric.metric_name == name)
    )
    return row.scalar_one_or_none()


async def _kpisByMetricId(session: AsyncSession, metricId: int) -> list[KpiCatalog]:
    rows = await session.execute(
        select(KpiCatalog).where(KpiCatalog.metric_id == metricId)
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# /metric → 自动 DRAFT 户头（完整 HTTP 链路）
# ---------------------------------------------------------------------------


class TestChatMetricCreatesDraftKpi:
    async def test_slash_metric_creates_draft_kpi_attached(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        await _seedDatasource(dbSession)
        resp = await client.post(
            "/api/v1/chat",
            json=_chatPayload("/metric 采购准时率 = 合格行数/总行数"),
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "采购准时率" in body["answer"]

        metric = await _getMetric(dbSession, "采购准时率")
        assert metric is not None, "/metric 应创建 ontology_metric"
        kpis = await _kpisByMetricId(dbSession, metric.id)
        assert len(kpis) == 1, "B5：metric 创建后应自动挂靠一条 KPI 户头"
        kpi = kpis[0]
        assert kpi.status == "DRAFT"
        assert kpi.kpi_code == f"KPI_AUTO_M{metric.id}"
        assert kpi.kpi_name == "采购准时率"
        # created_by 归属真实调用方（user 经 _handleDomainCommand 透传）
        assert kpi.created_by == "metric-draft-tester"
        # 自动户头尚无数据源可绑（人工治理动作后置）
        assert kpi.datasource_id is None
        # 答案尾注回显户头编码，用户可感知
        assert kpi.kpi_code in body["answer"]

    async def test_create_failure_does_not_break_metric_answer(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """户头创建异常 → 指标本体答案不受影响（best-effort 只降级尾注）。"""
        await _seedDatasource(dbSession)

        async def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated draft creation failure")

        monkeypatch.setattr(KpiCatalogService, "ensureDraftForMetric", _boom)
        resp = await client.post(
            "/api/v1/chat",
            json=_chatPayload("/metric 降级指标 = SUM(x)/COUNT(*)"),
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert "降级指标" in resp.json()["answer"]
        metric = await _getMetric(dbSession, "降级指标")
        assert metric is not None
        assert await _kpisByMetricId(dbSession, metric.id) == []


# ---------------------------------------------------------------------------
# ensureDraftForMetric 幂等（service 级）
# ---------------------------------------------------------------------------


class TestEnsureDraftForMetricIdempotent:
    async def test_second_call_returns_same_row(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        metric = OntologyMetric(metric_name="幂等指标", formula="SUM(x)/COUNT(*)")
        dbSession.add(metric)
        await dbSession.commit()

        svc = KpiCatalogService()
        first = await svc.ensureDraftForMetric(dbSession, metric, actor="tester-idem")
        second = await svc.ensureDraftForMetric(dbSession, metric, actor="tester-idem")

        assert first.id == second.id
        assert len(await _kpisByMetricId(dbSession, metric.id)) == 1

    async def test_generated_code_collision_gets_timestamp_suffix(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """KPI_AUTO_M{id} 被手工占用 → 时间戳后缀兜底，不抛冲突。"""
        metric = OntologyMetric(metric_name="撞码指标", formula="SUM(x)")
        dbSession.add(metric)
        await dbSession.commit()
        # 预占确定性 code
        dbSession.add(
            KpiCatalog(
                kpi_code=f"KPI_AUTO_M{metric.id}",
                kpi_name="预占户头",
                status="DRAFT",
            )
        )
        await dbSession.commit()

        svc = KpiCatalogService()
        entity = await svc.ensureDraftForMetric(dbSession, metric, actor="tester-collide")
        assert entity.kpi_code.startswith(f"KPI_AUTO_M{metric.id}_")
        # 预占行未被复用（metric_id 为空）
        assert entity.metric_id == metric.id


# ---------------------------------------------------------------------------
# grain 粒度弱校验（warning 不阻断）
# ---------------------------------------------------------------------------


class TestGrainMismatchWarning:
    async def test_non_covering_grain_warns_but_saves(
        self, client: AsyncClient, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        metric = OntologyMetric(
            metric_name="粒度校验指标",
            formula="SUM(x)",
            dimension_defaults={"month": "last", "supplier": "all"},
        )
        dbSession.add(metric)
        await dbSession.commit()
        await _seedDatasource(dbSession, 9702)

        svc = KpiCatalogService()
        dto = KpiCatalogCreate(
            kpi_code="KPI_GRAIN_TEST",
            kpi_name="粒度测试",
            metric_id=metric.id,
            grain="按月",
            datasource_id=9702,
        )
        with caplog.at_level(logging.WARNING, logger="app.services.kpi_catalog_service"):
            entity = await svc.createKpi(
                dbSession, dto, actor=CurrentUser(userId="grain-tester", roles=("admin",))
            )
        assert entity.id is not None, "grain 不一致只警告，不阻断保存"
        assert any(
            "dimension_defaults" in rec.message and "supplier" in rec.message
            for rec in caplog.records
        ), "应警告 grain 未覆盖的维度键"

    async def test_covering_grain_does_not_warn(
        self, client: AsyncClient, dbSession: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        metric = OntologyMetric(
            metric_name="粒度一致指标",
            formula="SUM(x)",
            dimension_defaults={"month": "last"},
        )
        dbSession.add(metric)
        await dbSession.commit()
        await _seedDatasource(dbSession, 9703)

        svc = KpiCatalogService()
        dto = KpiCatalogCreate(
            kpi_code="KPI_GRAIN_OK",
            kpi_name="粒度一致",
            metric_id=metric.id,
            grain="按月统计",
            datasource_id=9703,
        )
        with caplog.at_level(logging.WARNING, logger="app.services.kpi_catalog_service"):
            await svc.createKpi(
                dbSession, dto, actor=CurrentUser(userId="grain-tester", roles=("admin",))
            )
        assert not any("dimension_defaults" in rec.message for rec in caplog.records)
