"""P1a: GET /api/v1/kpi-catalog/{id}/datasource-suggestions 数据源绑定建议。

覆盖三类信号 + 边界：
- legacy 名称精确（0.9）/ 模糊（0.55）
- ontology 表反查（metric → class.source_table → schema_cache）（0.8）
- 停用数据源排除 / 无信号空数组 / 未知 KPI 404 / 最多 5 条
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DataSource,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    SchemaCache,
)

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


async def _addSource(
    dbSession: AsyncSession, name: str, *, isActive: bool = True
) -> DataSource:
    ds = DataSource(
        name=name,
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=isActive,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _addKpi(
    dbSession: AsyncSession,
    code: str,
    *,
    legacyName: str | None = None,
    metricId: int | None = None,
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        version="1",
        semantic_keywords=[],
        match_threshold=Decimal("0.5"),
        data_source=legacyName,
        metric_id=metricId,
        datasource_id=None,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


async def _suggestions(client: AsyncClient, kpiId: int) -> list[dict]:
    resp = await client.get(
        f"/api/v1/kpi-catalog/{kpiId}/datasource-suggestions", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_exact_legacy_name_match(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _addSource(dbSession, "SUGGEST_EXACT_DS")
    kpi = await _addKpi(dbSession, "SUG_KPI_EXACT", legacyName="suggest_exact_ds")

    rows = await _suggestions(client, kpi.id)

    assert rows[0]["datasourceId"] == ds.id
    assert rows[0]["name"] == "SUGGEST_EXACT_DS"
    assert rows[0]["score"] == pytest.approx(0.90)
    assert rows[0]["reasons"] == ["legacy_name_exact"]


@pytest.mark.asyncio
async def test_fuzzy_legacy_name_match(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _addSource(dbSession, "SUGGEST_FUZZY_TARGET")
    kpi = await _addKpi(dbSession, "SUG_KPI_FUZZY", legacyName="Fuzzy")

    rows = await _suggestions(client, kpi.id)

    hit = next(r for r in rows if r["datasourceId"] == ds.id)
    assert hit["score"] == pytest.approx(0.55)
    assert hit["reasons"] == ["legacy_name_fuzzy"]


@pytest.mark.asyncio
async def test_ontology_table_lookup(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """metric → class.source_table → schema_cache 持有该表的数据源（0.8）。"""
    ds = await _addSource(dbSession, "SUGGEST_ONTOLOGY_DS")
    ontologyClass = OntologyClass(class_name="SupplierDim", source_table="SUGGEST_DIM_SUPPLIER")
    dbSession.add(ontologyClass)
    await dbSession.commit()
    await dbSession.refresh(ontologyClass)
    metric = OntologyMetric(
        metric_name="SUGGEST_METRIC",
        formula="SUM(x)",
        agg_function="SUM",
        target_class_id=ontologyClass.id,
    )
    dbSession.add(metric)
    await dbSession.commit()
    await dbSession.refresh(metric)
    dbSession.add(
        SchemaCache(
            datasource_id=ds.id,
            schema_name="",
            schema_data=[{"table_name": "suggest_dim_supplier"}],
            schema_version="md5-suggest",
        )
    )
    await dbSession.commit()
    kpi = await _addKpi(dbSession, "SUG_KPI_ONTOLOGY", metricId=metric.id)

    rows = await _suggestions(client, kpi.id)

    hit = next(r for r in rows if r["datasourceId"] == ds.id)
    assert hit["score"] == pytest.approx(0.80)
    assert hit["reasons"] == ["ontology_table"]


@pytest.mark.asyncio
async def test_inactive_source_excluded(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _addSource(dbSession, "SUGGEST_INACTIVE_DS", isActive=False)
    kpi = await _addKpi(dbSession, "SUG_KPI_INACTIVE", legacyName="SUGGEST_INACTIVE_DS")

    assert await _suggestions(client, kpi.id) == []


@pytest.mark.asyncio
async def test_no_signal_returns_empty(client: AsyncClient, dbSession: AsyncSession) -> None:
    kpi = await _addKpi(dbSession, "SUG_KPI_NONE", legacyName="不存在的数据源名")

    assert await _suggestions(client, kpi.id) == []


@pytest.mark.asyncio
async def test_unknown_kpi_returns_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    resp = await client.get(
        "/api/v1/kpi-catalog/999999/datasource-suggestions", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_capped_at_five_suggestions(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    for i in range(6):
        await _addSource(dbSession, f"SUGGEST_CAP_{i}_DS")
    kpi = await _addKpi(dbSession, "SUG_KPI_CAP", legacyName="SUGGEST_CAP")

    rows = await _suggestions(client, kpi.id)

    assert len(rows) == 5
