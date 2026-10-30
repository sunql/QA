"""I4-I5 datasource_service.delete 引用计数（per F2 spec §5.3）。"""
from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError
from app.domain.models import DataSource, KpiCatalog
from app.domain.schemas import KpiCatalogUpdate
from app.services.datasource_service import DataSourceService
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.messages_zh import MSG_DATASOURCE_HAS_KPI_BINDINGS


async def _addKpi(
    dbSession: AsyncSession,
    code: str,
    datasourceId: int,
    *,
    status: str = "DRAFT",
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        version="1",
        status=status,
        semantic_keywords=[],
        match_threshold=Decimal("0.5"),
        datasource_id=datasourceId,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


@pytest.mark.asyncio
async def test_deleteDatasource_with_no_kpi_refs_succeeds(
    dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """I4: 无 KPI 引用 → 204 + DS 行消失。"""
    svc = DataSourceService()
    ds_id = seed_data_source.id
    await svc.delete(dbSession, ds_id)
    # Verify DS 行消失
    ds_after = await dbSession.get(DataSource, ds_id)
    assert ds_after is None


@pytest.mark.asyncio
async def test_deleteDatasource_with_kpi_ref_returns_409(
    dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """I5: 有 1 个 KPI 引用 → 409 + MSG_DATASOURCE_HAS_KPI_BINDINGS + DS 行保留。"""
    svc = DataSourceService()
    ds_id = seed_data_source.id
    with pytest.raises(ConflictError) as exc_info:
        await svc.delete(dbSession, ds_id)
    assert MSG_DATASOURCE_HAS_KPI_BINDINGS.format(id=ds_id, count=1) in str(exc_info.value)
    # DS 行保留
    ds_after = await dbSession.get(DataSource, ds_id)
    assert ds_after is not None


@pytest.mark.asyncio
async def test_deleteDatasource_with_multiple_kpi_refs_reports_exact_count(
    dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """P2a: 同一 DS 绑 2 个 KPI → 消息里 count 精确为 2（不是布尔式 1）。"""
    await _addKpi(dbSession, "KPI_GUARD_SECOND", seed_data_source.id)
    svc = DataSourceService()
    ds_id = seed_data_source.id
    with pytest.raises(ConflictError) as exc_info:
        await svc.delete(dbSession, ds_id)
    assert MSG_DATASOURCE_HAS_KPI_BINDINGS.format(id=ds_id, count=2) in str(exc_info.value)
    assert await dbSession.get(DataSource, ds_id) is not None


@pytest.mark.asyncio
async def test_deleteDatasource_blocks_deprecated_kpi(
    dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """P2a: 守卫与 KPI 状态无关 —— DEPRECATED 的 KPI 仍阻止删除。"""
    await _addKpi(dbSession, "KPI_GUARD_DEPRECATED", seed_data_source.id, status="DEPRECATED")
    svc = DataSourceService()
    ds_id = seed_data_source.id
    with pytest.raises(ConflictError):
        await svc.delete(dbSession, ds_id)
    assert await dbSession.get(DataSource, ds_id) is not None


@pytest.mark.asyncio
async def test_deleteDatasource_route_returns_409(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """P2a: HTTP 链路 —— DELETE /api/v1/datasources/{id} → 409 + 引用文案。"""
    ds_id = seed_data_source.id
    resp = await client.delete(
        f"/api/v1/datasources/{ds_id}",
        headers={"X-User-Id": "guard-admin", "X-User-Roles": "admin"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    error = body.get("error", "")
    assert "KPI" in error
    assert "1 个" in error
    # DS 未被删除（populate_existing：绕过本地 identity map 的缓存对象）
    still = (
        await dbSession.execute(
            select(DataSource)
            .where(DataSource.id == ds_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    assert still is not None


@pytest.mark.asyncio
async def test_deleteDatasource_allowed_after_unbind(
    dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """P2a: 计数是实时查询 —— KPI 改绑其他 DS 后原 DS 可删。"""
    other = DataSource(
        name="GUARD_OTHER_DS",
        type="mysql",
        host="x", port=3306,
        database_name="x", username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(other)
    await dbSession.commit()
    await dbSession.refresh(other)

    actor = CurrentUser(userId="guard-admin", roles=("admin",))
    await KpiCatalogService().updateKpi(
        dbSession,
        sample_kpi.id,
        KpiCatalogUpdate(datasource_id=other.id),
        actor,
    )

    ds_id = seed_data_source.id
    await DataSourceService().delete(dbSession, ds_id)
    assert await dbSession.get(DataSource, ds_id) is None
    # KPI 仍存在且绑定 other
    rebound = await dbSession.get(KpiCatalog, sample_kpi.id)
    assert rebound is not None
    assert rebound.datasource_id == other.id