"""SchemaIntrospectionService 单元测试（5.7）。

mock 适配器，验证：
- Oracle：ALL_TAB_COLUMNS/ALL_CONSTRAINTS 结果组装为 表/列/主键/外键/owner
- owner 规范化（大写 + 字符白名单，防注入）
- PostgreSQL / MySQL：information_schema（CURRENT_SCHEMA / DATABASE）
- 不支持的数据源类型抛 ValidationError
- 业务库连接/执行异常包装为 DataSourceError
- schema 版本 MD5 稳定且随数据变化
缓存持久化测试（触 DB）已迁至 integration/test_schema_introspection_cache.py
（【迁移：真实 PG】第三批）。
"""

from __future__ import annotations

import pytest

import app.services.schema_introspection_service as schema_module
from app.domain.exceptions import DataSourceError, ValidationError
from app.domain.models import DataSource
from app.domain.schemas import ForeignKeySchemaRead
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


class TestOracleIntrospection:
    async def test_assemble_columns_pks_fks(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "PRECEIPT", "column_name": "PTHNUM_0", "data_type": "VARCHAR2", "nullable": 0, "owner": "ZJTH"},
                {"table_name": "PRECEIPT", "column_name": "TOTQTY_0", "data_type": "NUMBER", "nullable": 1, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "PRECEIPT", "column_name": "PTHNUM_0"}],
            fkRows=[
                {"table_name": "PRECEIPTD", "column_name": "PTHNUM_0", "ref_table": "PRECEIPT", "ref_column": "PTHNUM_0"},
            ],
        )
        tables = await _service(adapter).introspect(_datasource())

        assert [t.table_name for t in tables] == ["PRECEIPT", "PRECEIPTD"]
        receipt = tables[0]
        assert receipt.owner == "ZJTH"
        assert [(c.column_name, c.data_type, c.nullable) for c in receipt.columns] == [
            ("PTHNUM_0", "VARCHAR2", False),
            ("TOTQTY_0", "NUMBER", True),
        ]
        assert receipt.primary_keys == ["PTHNUM_0"]
        assert receipt.foreign_keys == []

        receiptd = tables[1]
        assert receiptd.foreign_keys == [
            ForeignKeySchemaRead(column_name="PTHNUM_0", ref_table="PRECEIPT", ref_column="PTHNUM_0"),
        ]
        # 列查询 / 主键查询 / 外键查询各执行一次，共 3 条只读 SQL
        assert len(adapter.executed) == 3

    async def test_owner_uppercased_and_inlined_safely(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[{"table_name": "T", "column_name": "ID", "data_type": "NUMBER", "nullable": 0, "owner": "ZJTH"}],
            pkRows=[{"table_name": "T", "column_name": "ID"}],
            fkRows=[],
        )
        await _service(adapter).introspect(_datasource(username="zjth"))
        # 用户名规范化为大写 owner 字面量，注入到查询
        assert any("WHERE owner = 'ZJTH'" in sql for sql in adapter.executed)

    async def test_invalid_owner_raises_validation_error(self) -> None:
        adapter = _FakeSchemaAdapter(columnRows=[], pkRows=[], fkRows=[])
        with pytest.raises(ValidationError):
            await _service(adapter).introspect(_datasource(username="bad;owner"))
        # 校验失败时不触达业务库
        assert adapter.executed == []


class TestInfoSchemaIntrospection:
    async def test_postgres_queries_current_schema(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "orders", "column_name": "id", "data_type": "integer", "nullable": 0, "owner": "public"},
                {"table_name": "order_items", "column_name": "order_id", "data_type": "integer", "nullable": 0, "owner": "public"},
            ],
            pkRows=[{"table_name": "orders", "column_name": "id"}],
            fkRows=[
                {"table_name": "order_items", "column_name": "order_id", "ref_table": "orders", "ref_column": "id"},
            ],
        )
        tables = await _service(adapter).introspect(_datasource(type_="postgresql"))
        # 按表名排序："order_items" 在 "orders" 前
        assert [t.table_name for t in tables] == ["order_items", "orders"]
        assert tables[1].owner == "public"
        assert tables[0].foreign_keys[0].ref_table == "orders"
        assert any("CURRENT_SCHEMA()" in sql for sql in adapter.executed)

    async def test_mysql_queries_database(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "wms_order", "column_name": "id", "data_type": "int", "nullable": 0, "owner": "wms"},
            ],
            pkRows=[{"table_name": "wms_order", "column_name": "id"}],
            fkRows=[],
        )
        tables = await _service(adapter).introspect(_datasource(type_="mysql"))
        assert tables[0].table_name == "wms_order"
        assert any("DATABASE()" in sql for sql in adapter.executed)

    @pytest.mark.parametrize("type_", ["mysql", "postgresql"])
    async def test_fk_query_reads_referenced_columns_from_key_column_usage(self, type_: str) -> None:
        """外键查询从 key_column_usage 直接读 referenced_* 列，不依赖 MariaDB 专有的 constraint_column_usage 视图。

        MySQL 8 没有 information_schema.constraint_column_usage（1109 Unknown table），该视图仅存在于 MariaDB；
        PG/MySQL 的 key_column_usage 均含 referenced_table_name / referenced_column_name。
        """
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "wms_order_item", "column_name": "order_id", "data_type": "int", "nullable": 1, "owner": "wms"},
            ],
            pkRows=[],
            fkRows=[
                {"table_name": "wms_order_item", "column_name": "order_id", "ref_table": "wms_order", "ref_column": "id"},
            ],
        )
        tables = await _service(adapter).introspect(_datasource(type_=type_))
        fkSql = next(sql for sql in adapter.executed if "FOREIGN KEY" in sql)
        assert "constraint_column_usage" not in fkSql.lower()
        assert "referenced_table_name" in fkSql.lower()
        assert "referenced_column_name" in fkSql.lower()
        assert tables[0].foreign_keys[0].ref_table == "wms_order"
        assert tables[0].foreign_keys[0].ref_column == "id"

    async def test_unsupported_type_raises_validation_error(self) -> None:
        adapter = _FakeSchemaAdapter(columnRows=[], pkRows=[], fkRows=[])
        with pytest.raises(ValidationError):
            await _service(adapter).introspect(_datasource(type_="sqlserver"))


class TestIntrospectionFailure:
    async def test_adapter_error_wrapped_without_leaking_detail(self) -> None:
        class _BoomAdapter:
            async def execute_read_only(self, sql: str) -> list[dict]:
                raise ConnectionError("连不上业务库")

        service = SchemaIntrospectionService(adapterProvider=lambda datasourceId, ds: _BoomAdapter())
        with pytest.raises(DataSourceError) as excInfo:
            await service.introspect(_datasource())
        assert "读取失败" in excInfo.value.message
        # 底层异常文本不泄露到对外 detail（防 DB 用户名/DSN 等敏感信息）
        assert "连不上业务库" not in (excInfo.value.detail or "")
        assert "服务端日志" in excInfo.value.detail

    async def test_table_count_over_cap_raises_data_source_error(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {
                    "table_name": f"T{i:04d}",
                    "column_name": "ID",
                    "data_type": "NUMBER",
                    "nullable": 0,
                    "owner": "ZJTH",
                }
                for i in range(schema_module.MAX_SCHEMA_TABLES + 1)
            ],
            pkRows=[],
            fkRows=[],
        )
        with pytest.raises(DataSourceError) as excInfo:
            await _service(adapter).introspect(_datasource())
        assert "超过上限" in excInfo.value.message


class TestSchemaVersion:
    def test_stable_for_same_data(self) -> None:
        a = [{"table_name": "A", "owner": "ZJTH", "columns": []}]
        assert schema_module._schemaVersion(a) == schema_module._schemaVersion(a)

    def test_changes_with_data(self) -> None:
        assert schema_module._schemaVersion([{"table_name": "A"}]) != schema_module._schemaVersion(
            [{"table_name": "B"}]
        )
