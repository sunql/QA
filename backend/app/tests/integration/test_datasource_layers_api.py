"""GET /datasources/{id}/layers 数据层分布端点集成测试（方案 2）.

钉死：
- cache-only：schema_cache 行存在 → 前缀规则分类 + unknownTables 透出
- cache miss → fromCache=false（不触发实时探库）
- 数据源不存在 → 404
"""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, SchemaCache

pytestmark = pytest.mark.asyncio


async def _ensureDataSourceAndSchema(
    dbSession: AsyncSession, tables: list[str]
) -> int:
    ds = DataSource(
        name=f"layers-{uuid.uuid4().hex[:8]}", type="POSTGRESQL",
        host="localhost", port=5433, database_name="qa_metadata_test",
        username="qa_user", password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.flush()
    schemaData = [
        {"table_name": t, "owner": "", "columns": [],
         "primary_keys": [], "foreign_keys": []}
        for t in tables
    ]
    dbSession.add(
        SchemaCache(datasource_id=ds.id, schema_data=schemaData, schema_version="v1")
    )
    await dbSession.commit()
    return ds.id


async def _ensureDataSourceOnly(dbSession: AsyncSession) -> int:
    ds = DataSource(
        name=f"layers-nocache-{uuid.uuid4().hex[:8]}", type="POSTGRESQL",
        host="localhost", port=5433, database_name="qa_metadata_test",
        username="qa_user", password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds.id


async def test_layers_classified_by_prefix(client: AsyncClient, dbSession: AsyncSession) -> None:
    dsId = await _ensureDataSourceAndSchema(
        dbSession,
        tables=[
            "ODS_DIM_SUPPLIER",   # ODS_DICT（长前缀优先）
            "ODS_PO_HEADER",      # ODS_BUSINESS
            "DIM_MATERIAL",       # DIM
            "ADS_SUPPLIER_360",   # ADS
            "BPSCUST",            # 无前缀 → unknown
        ],
    )
    resp = await client.get(f"/api/v1/datasources/{dsId}/layers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["datasourceId"] == dsId
    assert body["fromCache"] is True
    assert body["totalTables"] == 5
    assert body["unknownTables"] == 1
    counts = {c["layer"]: c["tableCount"] for c in body["layers"]}
    assert counts == {"ODS_DICT": 1, "ODS_BUSINESS": 1, "DIM": 1, "ADS": 1}


async def test_layers_cache_miss_returns_from_cache_false(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """无 schema_cache 行 → fromCache=false 空载荷（不实时探库）。"""
    dsId = await _ensureDataSourceOnly(dbSession)
    resp = await client.get(f"/api/v1/datasources/{dsId}/layers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fromCache"] is False
    assert body["totalTables"] == 0
    assert body["layers"] == []


async def test_layers_unknown_datasource_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/datasources/99999999/layers")
    assert resp.status_code == 404
