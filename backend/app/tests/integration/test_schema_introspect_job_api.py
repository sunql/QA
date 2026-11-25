"""异步内省 job：成功写缓存 / 失败不写 / 状态流转。"""

from __future__ import annotations

import pytest

from app.services import schema_introspect_job as jobModule
from app.services.schema_introspect_job import SchemaIntrospectJobRegistry


@pytest.mark.asyncio
async def test_run_job_success_writes_cache(monkeypatch, client) -> None:
    """成功路径：job 变 succeeded，schema 缓存可读。"""
    resp = await client.post(
        "/api/v1/datasources",
        json={
            "name": "ds", "type": "oracle", "host": "h", "port": 1521,
            "databaseName": "X", "username": "LC019999", "password": "p",
            "isActive": True, "isDefault": False,
        },
    )
    dsId = resp.json()["id"]

    class _OkAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            if "SELECT table_name FROM ALL_TABLES" in sql:
                return [{"table_name": "PRECEIPT"}]
            if "ALL_TAB_COLUMNS" in sql:
                return [{
                    "table_name": "PRECEIPT", "column_name": "COL1",
                    "data_type": "VARCHAR2", "nullable": 0, "owner": "LC019999",
                }]
            return []

    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _OkAdapter())
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName="LC019999")

    await jobModule.runIntrospectJob(
        datasourceId=dsId, schemaName="LC019999", jobId=job.jobId, registry=registry
    )

    final = registry.get(job.jobId)
    assert final.status == "succeeded"
    assert final.total == 1 and final.completed == 1

    cached = await client.get(f"/api/v1/datasources/{dsId}/schema?schema=LC019999")
    assert cached.status_code == 200
    assert cached.json()["tables"][0]["tableName"] == "PRECEIPT"


@pytest.mark.asyncio
async def test_run_job_failure_marks_failed_and_writes_nothing(monkeypatch, client) -> None:
    """失败路径：job failed + error；schema_cache 不得出现半成品。"""
    resp = await client.post(
        "/api/v1/datasources",
        json={
            "name": "ds2", "type": "oracle", "host": "h", "port": 1521,
            "databaseName": "X", "username": "LC019999", "password": "p",
            "isActive": True, "isDefault": False,
        },
    )
    dsId = resp.json()["id"]

    class _BoomAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            raise ConnectionError("业务库不可达")

    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _BoomAdapter())
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName="LC019999")

    await jobModule.runIntrospectJob(
        datasourceId=dsId, schemaName="LC019999", jobId=job.jobId, registry=registry
    )

    final = registry.get(job.jobId)
    assert final.status == "failed"
    assert final.error and "读取失败" in final.error

    cached = await client.get(f"/api/v1/datasources/{dsId}/schema?schema=LC019999")
    assert cached.status_code == 404, "失败不得写缓存"
