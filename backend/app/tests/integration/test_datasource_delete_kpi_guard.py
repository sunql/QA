"""I4-I5 datasource_service.delete 引用计数（per F2 spec §5.3）。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.models import DataSource, KpiCatalog
from app.services.datasource_service import DataSourceService
from app.services.messages_zh import MSG_DATASOURCE_HAS_KPI_BINDINGS


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