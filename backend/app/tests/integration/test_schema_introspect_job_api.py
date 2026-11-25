"""异步内省 job：成功写缓存 / 失败不写 / 取消落终态 / 状态流转。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.domain.models import SchemaCache
from app.services import schema_introspect_job as jobModule
from app.services.schema_introspect_job import SchemaIntrospectJobRegistry

_SCHEMA_NAME = "LC019999"


def _isColumnQuery(sql: str) -> bool:
    """列查询：ALL_TAB_COLUMNS 但**不是**表名列表（后者是 SELECT DISTINCT）。"""
    return "ALL_TAB_COLUMNS" in sql and "SELECT DISTINCT table_name" not in sql


async def _createDatasource(client, name: str) -> int:
    resp = await client.post(
        "/api/v1/datasources",
        json={
            "name": name, "type": "oracle", "host": "h", "port": 1521,
            "databaseName": "X", "username": _SCHEMA_NAME, "password": "p",
            "isActive": True, "isDefault": False,
        },
    )
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_run_job_success_writes_cache(monkeypatch, client) -> None:
    """成功路径：job 变 succeeded，schema 缓存可读。"""
    dsId = await _createDatasource(client, "ds")

    class _OkAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute_read_only(self, sql: str) -> list[dict]:
            self.calls.append(sql)
            if "SELECT DISTINCT table_name" in sql:
                return [{"table_name": "PRECEIPT"}]
            if "ALL_TAB_COLUMNS" in sql:
                return [{
                    "table_name": "PRECEIPT", "column_name": "COL1",
                    "data_type": "VARCHAR2", "nullable": 0, "owner": _SCHEMA_NAME,
                }]
            return []

    adapter = _OkAdapter()
    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: adapter)
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName=_SCHEMA_NAME)

    await jobModule.runIntrospectJob(
        datasourceId=dsId, schemaName=_SCHEMA_NAME, jobId=job.jobId, registry=registry
    )

    final = registry.get(job.jobId)
    assert final.status == "succeeded"
    assert final.total == 1 and final.completed == 1
    assert any("SELECT DISTINCT table_name" in sql for sql in adapter.calls), "必须查过表名列表"
    assert any(_isColumnQuery(sql) for sql in adapter.calls), "必须查过列元数据"

    cached = await client.get(f"/api/v1/datasources/{dsId}/schema?schema={_SCHEMA_NAME}")
    assert cached.status_code == 200
    assert cached.json()["tables"][0]["tableName"] == "PRECEIPT"


@pytest.mark.asyncio
async def test_run_job_failure_marks_failed_and_writes_nothing(
    monkeypatch, client, dbSession
) -> None:
    """任一批失败 ⇒ job failed 且 schema_cache 不落任何行（不允许半成品）。

    表名列表与列查询正常返回（2 张表），PK 查询（ALL_CONSTRAINTS）抛错：
    实现若「每批落一次库」，第一批已写的内容会让本用例变红。
    """
    dsId = await _createDatasource(client, "ds2")

    class _PkFailureAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute_read_only(self, sql: str) -> list[dict]:
            self.calls.append(sql)
            if "SELECT DISTINCT table_name" in sql:
                return [{"table_name": "PRECEIPT"}, {"table_name": "PRECEIPTD"}]
            if "ALL_CONSTRAINTS" in sql:
                raise ConnectionError("业务库不可达")
            if "ALL_TAB_COLUMNS" in sql:
                return [
                    {
                        "table_name": "PRECEIPT", "column_name": "COL1",
                        "data_type": "VARCHAR2", "nullable": 0, "owner": _SCHEMA_NAME,
                    },
                    {
                        "table_name": "PRECEIPTD", "column_name": "COL1",
                        "data_type": "VARCHAR2", "nullable": 0, "owner": _SCHEMA_NAME,
                    },
                ]
            return []

    adapter = _PkFailureAdapter()
    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: adapter)
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName=_SCHEMA_NAME)

    await jobModule.runIntrospectJob(
        datasourceId=dsId, schemaName=_SCHEMA_NAME, jobId=job.jobId, registry=registry
    )

    final = registry.get(job.jobId)
    assert final.status == "failed"
    assert final.error and "读取失败" in final.error
    # 不泄漏用户名 / 底层异常文本（只进服务端日志）
    assert _SCHEMA_NAME not in final.error
    assert "业务库不可达" not in final.error

    # 直接查库：失败不得留下任何缓存行（不依赖 404 的间接判断）
    assert any(_isColumnQuery(sql) for sql in adapter.calls), "失败发生在列查询之后"
    assert any("ALL_CONSTRAINTS" in sql for sql in adapter.calls), "PK 查询必须被尝试"
    rowCount = (
        await dbSession.execute(
            select(func.count())
            .select_from(SchemaCache)
            .where(
                SchemaCache.datasource_id == dsId,
                SchemaCache.schema_name == _SCHEMA_NAME,
            )
        )
    ).scalar_one()
    assert rowCount == 0, "任一批失败不得写缓存"

    cached = await client.get(f"/api/v1/datasources/{dsId}/schema?schema={_SCHEMA_NAME}")
    assert cached.status_code == 404, "失败不得写缓存"


@pytest.mark.asyncio
async def test_run_job_cancelled_marks_failed_and_reraises(monkeypatch, client) -> None:
    """取消（CancelledError）必须落 failed 后原样上抛，否则 job 永挂 running。"""
    dsId = await _createDatasource(client, "ds3")

    class _CancelledAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            raise asyncio.CancelledError()

    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _CancelledAdapter())
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName=_SCHEMA_NAME)

    with pytest.raises(asyncio.CancelledError):
        await jobModule.runIntrospectJob(
            datasourceId=dsId, schemaName=_SCHEMA_NAME, jobId=job.jobId, registry=registry
        )

    final = registry.get(job.jobId)
    assert final.status == "failed"
    assert final.error == "任务已取消"

    # running 死 job 会让 createOrGet 永久复用；落终态后应能再发起新 job。
    nextJob = registry.createOrGet(datasourceId=dsId, schemaName=_SCHEMA_NAME)
    assert nextJob.jobId != job.jobId
