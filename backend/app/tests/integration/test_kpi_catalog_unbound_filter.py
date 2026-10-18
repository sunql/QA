"""P2b: GET /api/v1/kpi-catalog?unbound=true 未绑定 KPI 报告接口。

契约：
- 不带参数 → 返回全部（已绑定 + 未绑定）
- ``unbound=true`` → 只返回 ``datasource_id IS NULL`` 的行
- 未绑定行响应的 ``datasourceId`` 为 null（前端据此渲染「未绑定」标签）

真实 PG + 完整 API 链路。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, KpiCatalog

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_UNBOUND_CODE = "KPI_UNBOUND_REPORT"
_BOUND_CODE = "KPI_BOUND_REPORT"


async def _addKpi(
    dbSession: AsyncSession, code: str, datasourceId: int | None
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
async def test_unbound_true_returns_only_null_datasource(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    await _addKpi(dbSession, _BOUND_CODE, seed_data_source.id)
    await _addKpi(dbSession, _UNBOUND_CODE, None)

    resp = await client.get(
        "/api/v1/kpi-catalog", params={"unbound": "true"}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    codes = [r["kpiCode"] for r in rows]
    assert _UNBOUND_CODE in codes
    assert _BOUND_CODE not in codes
    assert all(r["datasourceId"] is None for r in rows)


@pytest.mark.asyncio
async def test_default_list_includes_both(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    await _addKpi(dbSession, _BOUND_CODE, seed_data_source.id)
    await _addKpi(dbSession, _UNBOUND_CODE, None)

    resp = await client.get("/api/v1/kpi-catalog", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    by_code = {r["kpiCode"]: r for r in resp.json()}
    assert by_code[_BOUND_CODE]["datasourceId"] == seed_data_source.id
    assert by_code[_UNBOUND_CODE]["datasourceId"] is None


@pytest.mark.asyncio
async def test_unbound_false_equals_default(
    client: AsyncClient, dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """显式 unbound=false 与不带参数同语义（不是「取反」）。"""
    await _addKpi(dbSession, _BOUND_CODE, seed_data_source.id)
    await _addKpi(dbSession, _UNBOUND_CODE, None)

    resp = await client.get(
        "/api/v1/kpi-catalog", params={"unbound": "false"}, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    codes = {r["kpiCode"] for r in resp.json()}
    assert {_BOUND_CODE, _UNBOUND_CODE} <= codes
