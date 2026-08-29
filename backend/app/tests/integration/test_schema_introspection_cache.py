"""SchemaIntrospectionService schema 缓存持久化测试（5.7 缓存段）。

覆盖：introspectAndCache 写库 / 未变化复用同一行 / 变化时版本刷新并持久化。
组装类测试（mock 适配器，无 IO）留在 unit/test_schema_introspection_service.py。

【迁移：真实 PG】由 unit/ 迁至 integration/（第三批），dbSession 走 integration/conftest.py
的真实 PostgreSQL + 每测试 TRUNCATE 隔离（Harness/rules/测试规范.md）；业务库适配器为外部
依赖仍 mock（_FakeSchemaAdapter），SchemaCache/DataSource 数据层全真实。
"""

from __future__ import annotations

from sqlalchemy import func, select

import app.services.schema_introspection_service as schema_module
from app.domain.models import DataSource, SchemaCache
from app.services.schema_introspection_service import SchemaIntrospectionService


def _datasource(type_: str = "oracle", username: str = "ZJTH") -> DataSource:
    return DataSource(
        name="t",
        type=type_,
        host="db.example.com",
        port=1521,
        database_name="db",
        username=username,
        password_encrypted="enc",
        is_active=True,
        is_default=False,
    )


class _FakeSchemaAdapter:
    """按 SQL 内容返回预置行集的伪适配器，记录已执行的 SQL。"""

    def __init__(self, columnRows: list[dict], pkRows: list[dict], fkRows: list[dict]) -> None:
        self.columnRows = columnRows
        self.pkRows = pkRows
        self.fkRows = fkRows
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "ALL_TAB_COLUMNS" in sql or "information_schema.columns" in sql:
            return self.columnRows
        if "constraint_type = 'P'" in sql or "PRIMARY KEY" in sql:
            return self.pkRows
        return self.fkRows


def _service(adapter: _FakeSchemaAdapter) -> SchemaIntrospectionService:
    return SchemaIntrospectionService(adapterProvider=lambda datasourceId, ds: adapter)


async def _persistDatasource(session, type_: str) -> DataSource:
    ds = _datasource(type_=type_)
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


class TestSchemaCache:
    async def test_introspect_and_cache_writes_row(self, dbSession) -> None:
        ds = await _persistDatasource(dbSession, "oracle")
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        cache = await _service(adapter).introspectAndCache(dbSession, ds)

        assert cache.datasource_id == ds.id
        assert cache.schema_data[0]["table_name"] == "T"
        assert cache.schema_version == schema_module._schemaVersion(cache.schema_data)
        # 重新读取
        again = await _service(adapter).getCached(dbSession, ds.id)
        assert again is not None and again.id == cache.id

    async def test_cache_unchanged_reuses_existing_row(self, dbSession) -> None:
        ds = await _persistDatasource(dbSession, "oracle")
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        first = await _service(adapter).introspectAndCache(dbSession, ds)
        second = await _service(adapter).introspectAndCache(dbSession, ds)

        assert second.id == first.id
        count = await dbSession.execute(select(func.count()).select_from(SchemaCache))
        assert count.scalar_one() == 1

    async def test_cache_refreshes_when_schema_changes(self, dbSession) -> None:
        ds = await _persistDatasource(dbSession, "oracle")
        adapter1 = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        first = await _service(adapter1).introspectAndCache(dbSession, ds)
        v1 = first.schema_version

        # 表新增一列 → 版本变化，缓存刷新（同一行，内容更新）
        adapter2 = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
                {"table_name": "T", "column_name": "NAME", "data_type": "VARCHAR2", "nullable": 1, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        second = await _service(adapter2).introspectAndCache(dbSession, ds)

        # 同一数据源缓存行复用（id 不变），版本与内容均已刷新
        assert second.id == first.id
        assert second.schema_version != v1
        assert second.schema_version == schema_module._schemaVersion(second.schema_data)
        assert [c["column_name"] for c in second.schema_data[0]["columns"]] == ["ID", "NAME"]
        # 重新读库确认已持久化
        fresh = await _service(adapter2).getCached(dbSession, ds.id)
        assert fresh is not None and fresh.schema_version == second.schema_version
        assert len(fresh.schema_data[0]["columns"]) == 2
