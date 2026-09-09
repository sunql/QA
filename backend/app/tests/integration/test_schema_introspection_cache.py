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


class TestSchemaCacheOwnerScoping:
    """Oracle owner 作用域化：同数据源多份 schema 缓存互不覆盖。"""

    async def test_two_owners_coexist_and_are_keyed_by_schema(self, dbSession) -> None:
        ds = await _persistDatasource(dbSession, "oracle")  # username ZJTH
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        svc = _service(adapter)

        defaultRow = await svc.introspectAndCache(dbSession, ds)  # 默认 owner = ZJTH
        thbiRow = await svc.introspectAndCache(dbSession, ds, owner="THBI")

        assert defaultRow.schema_name == "ZJTH"
        assert thbiRow.schema_name == "THBI"
        assert thbiRow.id != defaultRow.id
        count = await dbSession.execute(select(func.count()).select_from(SchemaCache))
        assert count.scalar_one() == 2

        # 缺省读取命中默认 owner 行；显式 owner 命中各自行
        cachedDefault = await svc.getCached(dbSession, ds.id)
        assert cachedDefault is not None and cachedDefault.schema_name == "ZJTH"
        cachedThbi = await svc.getCached(dbSession, ds.id, owner="THBI")
        assert cachedThbi is not None and cachedThbi.schema_name == "THBI"

    async def test_default_read_resolves_oracle_owner_from_datasource(self, dbSession) -> None:
        ds = await _persistDatasource(dbSession, "oracle")  # username ZJTH
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        svc = _service(adapter)
        await svc.introspectAndCache(dbSession, ds)

        # 只传 datasource_id（无 ds 对象）：getCached 回查 data_source 解析默认 owner
        fresh = await SchemaIntrospectionService().getCached(dbSession, ds.id)
        assert fresh is not None and fresh.schema_name == "ZJTH"

    async def test_owner_is_normalized_before_cache_key_and_lookup(self, dbSession) -> None:
        """小写/带空白的 owner 在写缓存键与读缓存前归一为规范 owner（防重复行）。"""
        ds = await _persistDatasource(dbSession, "oracle")  # username ZJTH
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "THBI"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        svc = _service(adapter)

        lower = await svc.introspectAndCache(dbSession, ds, owner="thbi")
        assert lower.schema_name == "THBI"
        # 内省 SQL 字面量按规范化 owner 执行（非小写）
        assert any("owner = 'THBI'" in sql for sql in adapter.executed)

        # 大小写不同的读取命中同一行；再按大写内省复用同一行（不产生第二份缓存）
        cached = await svc.getCached(dbSession, ds.id, owner="thbi")
        assert cached is not None and cached.id == lower.id and cached.schema_name == "THBI"
        upper = await svc.introspectAndCache(dbSession, ds, owner="THBI")
        assert upper.id == lower.id
        count = await dbSession.execute(select(func.count()).select_from(SchemaCache))
        assert count.scalar_one() == 1

    async def test_empty_owner_string_keys_to_default_oracle_owner(self, dbSession) -> None:
        """Oracle 空串 owner 等同缺省 → 键为 UPPER(username)，而非 ''（'' 是非 Oracle 语义）。"""
        ds = await _persistDatasource(dbSession, "oracle")  # username ZJTH
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        svc = _service(adapter)

        row = await svc.introspectAndCache(dbSession, ds, owner="")
        assert row.schema_name == "ZJTH"
        cached = await svc.getCached(dbSession, ds.id, owner="")
        assert cached is not None and cached.schema_name == "ZJTH"
        count = await dbSession.execute(select(func.count()).select_from(SchemaCache))
        assert count.scalar_one() == 1

    async def test_non_oracle_ignores_owner_in_cache_key(self, dbSession) -> None:
        """PG/MySQL 即使显式传 owner，缓存键恒为 ''（连接默认），与 _queryByType 忽略 owner 对齐。"""
        ds = await _persistDatasource(dbSession, "postgresql")
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "t", "column_name": "id", "data_type": "integer", "nullable": 1},
            ],
            pkRows=[],
            fkRows=[],
        )
        svc = _service(adapter)

        row = await svc.introspectAndCache(dbSession, ds, owner="THBI")
        assert row.schema_name == ""
        cached = await svc.getCached(dbSession, ds.id, owner="THBI")
        assert cached is not None and cached.id == row.id
        default = await svc.getCached(dbSession, ds.id)
        assert default is not None and default.id == row.id
        count = await dbSession.execute(select(func.count()).select_from(SchemaCache))
        assert count.scalar_one() == 1
