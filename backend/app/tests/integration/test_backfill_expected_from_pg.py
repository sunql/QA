"""_expectedFromPg 的 PG 期望集（metric / kpi 段）真库断言。

真实 PostgreSQL（integration/conftest.py 的 dbSession ⇒ 每测试 TRUNCATE 全表）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.models import KpiCatalog, OntologyMetric
from scripts.backfill_milvus_embeddings import _expectedFromPg

pytestmark = pytest.mark.asyncio


async def test_expected_from_pg_covers_four_types(dbSession) -> None:
    """期望集含 metric 全表（DRAFT 也建）+ PUBLISHED-only 的 KPI + 三类 entityMaps。"""
    metric = OntologyMetric(
        metric_name="回填率",
        formula="SUM(x)/SUM(y)",
        agg_function="ratio",
        metric_alias="fillRate",
    )
    dbSession.add(metric)
    await dbSession.flush()

    published = KpiCatalog(
        kpi_code="KPI_BF_TEST_P",
        kpi_name="回填测试-已发布",
        status="PUBLISHED",
        metric_id=metric.id,
        match_threshold=Decimal("0.75"),
    )
    draft = KpiCatalog(
        kpi_code="KPI_BF_TEST_D",
        kpi_name="回填测试-草稿",
        status="DRAFT",
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(published)
    dbSession.add(draft)
    await dbSession.commit()
    await dbSession.refresh(metric)
    await dbSession.refresh(published)
    await dbSession.refresh(draft)

    expected, details, entityMaps = await _expectedFromPg(dbSession)

    # metric = 全表（D3：metric 维持现状，DRAFT 也建向量）
    assert metric.id in {r["id"] for r in expected["metric"]}
    metricDetail = next(d for d in details["metric"] if d["id"] == metric.id)
    assert metricDetail["metric_alias"] == "fillRate"
    assert metricDetail["agg_function"] == "ratio"
    assert metricDetail["name"] == "回填率"
    assert metricDetail["alias"] == "fillRate"
    assert metricDetail["description"] == "ratio"

    # kpi = PUBLISHED-only；alias 取挂靠 metric 的 metric_alias
    assert published.id in {r["id"] for r in expected["kpi"]}
    assert draft.id not in {r["id"] for r in expected["kpi"]}
    kpiDetail = next(d for d in details["kpi"] if d["id"] == published.id)
    assert kpiDetail["metricAlias"] == "fillRate"
    assert kpiDetail["name"] == "回填测试-已发布"
    assert kpiDetail["alias"] == "fillRate"
    assert kpiDetail["description"] == ""   # business_definition 未填 ⇒ 归一为 ""
    assert kpiDetail["kpi_code"] == "KPI_BF_TEST_P"

    # entityMaps 只有 class / property / metric 三键
    assert set(entityMaps) == {"class", "property", "metric"}


async def test_expected_from_pg_kpi_without_metric_has_empty_alias(dbSession) -> None:
    """KPI 未挂靠 metric ⇒ alias 归一为 ""（outerjoin 语义），仍进 PUBLISHED 期望集。"""
    orphan = KpiCatalog(
        kpi_code="KPI_BF_TEST_ORPHAN",
        kpi_name="回填测试-无指标",
        status="PUBLISHED",
        match_threshold=Decimal("0.75"),
    )
    dbSession.add(orphan)
    await dbSession.commit()
    await dbSession.refresh(orphan)

    expected, details, entityMaps = await _expectedFromPg(dbSession)

    assert orphan.id in {r["id"] for r in expected["kpi"]}
    orphanDetail = next(d for d in details["kpi"] if d["id"] == orphan.id)
    assert orphanDetail["alias"] == ""
    assert orphanDetail["metricAlias"] == ""
    assert orphanDetail["description"] == ""
    assert orphanDetail["kpi_code"] == "KPI_BF_TEST_ORPHAN"


async def test_expected_from_pg_includes_draft_metric(dbSession) -> None:
    """metric 期望集 = 全表（D3：metric 维持现状 ⇒ DRAFT 也建向量，不按 status 过滤）。"""
    draftMetric = OntologyMetric(
        metric_name="草稿指标",
        formula="SUM(z)",
        agg_function="SUM",
        metric_alias="draftAgg",
        status="DRAFT",
    )
    dbSession.add(draftMetric)
    await dbSession.commit()
    await dbSession.refresh(draftMetric)

    expected, _details, _entityMaps = await _expectedFromPg(dbSession)

    assert draftMetric.id in {r["id"] for r in expected["metric"]}
