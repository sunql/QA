"""P0: POST /api/v1/kpi-catalog/bulk-bind-datasource 批量绑定。

覆盖：
- 混合批次逐行结果（bound / skipped / failed 计数精确）
- 已绑其他数据源默认 failed，force=true 覆盖
- bound 行 revision_count +1 且产生 kpi_updated outbox 事件（复用 updateKpi）
- 非 admin → 403；超过 1000 行 → 422
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditOutbox, DataSource, KpiCatalog

ADMIN_HEADERS = {"X-User-Id": "bulk-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "bulk-user", "X-User-Roles": "user"}

_URL = "/api/v1/kpi-catalog/bulk-bind-datasource"


async def _addSource(dbSession: AsyncSession, name: str) -> DataSource:
    ds = DataSource(
        name=name,
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _reload(dbSession: AsyncSession, kpiId: int) -> KpiCatalog:
    """跨 session 重读：API 在自己 session 里 commit，本地 identity map 已过期。"""
    result = await dbSession.execute(
        select(KpiCatalog)
        .where(KpiCatalog.id == kpiId)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _addKpi(
    dbSession: AsyncSession, code: str, datasourceId: int | None = None
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        version="1",
        semantic_keywords=[],
        match_threshold=Decimal("0.5"),
        datasource_id=datasourceId,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


@pytest.mark.asyncio
async def test_mixed_batch_reports_row_results(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    other = await _addSource(dbSession, "BULK_OTHER_DS")
    kpiA = await _addKpi(dbSession, "BULK_KPI_A")
    kpiB = await _addKpi(dbSession, "BULK_KPI_B")
    kpiBound = await _addKpi(dbSession, "BULK_KPI_BOUND", other.id)

    resp = await client.post(
        _URL,
        json=[
            {"kpiCode": "BULK_KPI_A", "datasourceId": seed_data_source.id},
            {"kpiCode": "BULK_KPI_B", "datasourceId": seed_data_source.id},
            {"kpiCode": "BULK_KPI_MISSING", "datasourceId": seed_data_source.id},
            {"kpiCode": "BULK_KPI_BOUND", "datasourceId": seed_data_source.id},
        ],
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 4
    assert (body["bound"], body["skipped"], body["failed"]) == (2, 0, 2)
    byCode = {r["kpiCode"]: r for r in body["results"]}
    assert byCode["BULK_KPI_A"]["status"] == "bound"
    assert byCode["BULK_KPI_A"]["kpiId"] == kpiA.id
    assert byCode["BULK_KPI_MISSING"]["status"] == "failed"
    assert "不存在" in byCode["BULK_KPI_MISSING"]["error"]
    assert byCode["BULK_KPI_BOUND"]["status"] == "failed"
    assert "force=false" in byCode["BULK_KPI_BOUND"]["error"]

    # 写入生效：DB 行 + revision_count +1（API 走独立 session，populate_existing 重读）
    seedId = seed_data_source.id
    refreshed = await _reload(dbSession, kpiA.id)
    assert refreshed.datasource_id == seedId
    assert refreshed.revision_count == 1
    untouched = await _reload(dbSession, kpiBound.id)
    assert untouched.datasource_id == other.id

    # 审计链：bound 行产生 kpi_updated outbox 事件
    outbox = (
        await dbSession.execute(
            select(AuditOutbox).where(
                AuditOutbox.event_type == "kpi_updated",
                AuditOutbox.entity_id == kpiA.id,
            )
        )
    ).scalars().all()
    assert len(outbox) == 1
    assert outbox[0].actor == "bulk-admin"

    # 同批第二个 bound 行同样 +1
    assert (await _reload(dbSession, kpiB.id)).revision_count == 1


@pytest.mark.asyncio
async def test_force_overrides_existing_binding(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    other = await _addSource(dbSession, "BULK_FORCE_OTHER")
    kpi = await _addKpi(dbSession, "BULK_KPI_FORCE", other.id)

    resp = await client.post(
        _URL,
        params={"force": "true"},
        json=[{"kpiCode": "BULK_KPI_FORCE", "datasourceId": seed_data_source.id}],
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bound"] == 1
    seedId = seed_data_source.id
    refreshed = await _reload(dbSession, kpi.id)
    assert refreshed.datasource_id == seedId


@pytest.mark.asyncio
async def test_same_binding_is_skipped_without_side_effects(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    kpi = await _addKpi(dbSession, "BULK_KPI_SKIP", seed_data_source.id)

    resp = await client.post(
        _URL,
        json=[{"kpiCode": "BULK_KPI_SKIP", "datasourceId": seed_data_source.id}],
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == 1
    assert body["results"][0]["reason"] == "already_bound"
    assert (await dbSession.get(KpiCatalog, kpi.id)).revision_count == 0


@pytest.mark.asyncio
async def test_non_admin_forbidden(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    resp = await client.post(
        _URL,
        json=[{"kpiCode": "BULK_KPI_ANY", "datasourceId": seed_data_source.id}],
        headers=USER_HEADERS,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_over_row_limit_rejected(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    items = [
        {"kpiCode": f"BULK_OVER_{i}", "datasourceId": seed_data_source.id}
        for i in range(1001)
    ]
    resp = await client.post(_URL, json=items, headers=ADMIN_HEADERS)
    assert resp.status_code == 422, resp.text
    assert "1000" in resp.text
