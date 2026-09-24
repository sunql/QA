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

from types import SimpleNamespace

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
    """按 SQL 内容返回预置行集的伪适配器，记录已执行的 SQL。

    feat-ontology-import-comment：增加 tableCommentRows / colCommentRows；
    按 SQL 关键字分发到 ALL_TAB_COMMENTS / ALL_COL_COMMENTS / pg_description /
    information_schema.tables.table_comment / information_schema.columns.column_comment。
    """

    def __init__(
        self,
        columnRows: list[dict] | None = None,
        pkRows: list[dict] | None = None,
        fkRows: list[dict] | None = None,
        ownerRows: list[dict] | None = None,
        tableCommentRows: list[dict] | None = None,
        colCommentRows: list[dict] | None = None,
    ) -> None:
        self.columnRows = columnRows or []
        self.pkRows = pkRows or []
        self.fkRows = fkRows or []
        self.ownerRows = ownerRows or []
        self.tableCommentRows = tableCommentRows or []
        self.colCommentRows = colCommentRows or []
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "ALL_TABLES" in sql:
            return self.ownerRows
        if "ALL_TAB_COMMENTS" in sql:
            return self.tableCommentRows
        if "ALL_COL_COMMENTS" in sql:
            return self.colCommentRows
        if "ALL_TAB_COLUMNS" in sql or "information_schema.columns" in sql:
            # PG/MySQL 列注释查询也走 information_schema.columns，特征是「带 column_comment」
            if "column_comment" in sql:
                return self.colCommentRows
            return self.columnRows
        if "pg_description" in sql and "objsubid = 0" in sql:
            return self.tableCommentRows
        if "pg_description" in sql and "objsubid = a.attnum" in sql:
            return self.colCommentRows
        if "information_schema.tables" in sql and "table_comment" in sql:
            return self.tableCommentRows
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
        # 列查询 / 主键查询 / 外键查询 + 表注释 + 列注释，共 5 条只读 SQL
        # （feat-ontology-import-comment：加了 ALL_TAB_COMMENTS / ALL_COL_COMMENTS）
        assert len(adapter.executed) == 5

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

    async def test_table_count_over_cap_raises_data_source_error(self, monkeypatch) -> None:
        # 上限从 getSettings().schema_max_tables 读取（可配置，env SCHEMA_MAX_TABLES）。
        # 这里把上限压到 5，塞 6 张表触发 guard，验证超限即拦截。
        monkeypatch.setattr(
            schema_module,
            "getSettings",
            lambda: SimpleNamespace(schemaMaxTables=5),
        )
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {
                    "table_name": f"T{i:04d}",
                    "column_name": "ID",
                    "data_type": "NUMBER",
                    "nullable": 0,
                    "owner": "ZJTH",
                }
                for i in range(6)
            ],
            pkRows=[],
            fkRows=[],
        )
        with pytest.raises(DataSourceError) as excInfo:
            await _service(adapter).introspect(_datasource())
        assert "超过上限" in excInfo.value.message
        assert "6" in excInfo.value.message  # 报错中带实际表数
        assert "5" in excInfo.value.message  # 报错中带上限

    async def test_table_count_within_limit_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr(
            schema_module,
            "getSettings",
            lambda: SimpleNamespace(schemaMaxTables=5),
        )
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {
                    "table_name": f"T{i:04d}",
                    "column_name": "ID",
                    "data_type": "NUMBER",
                    "nullable": 0,
                    "owner": "ZJTH",
                }
                for i in range(5)
            ],
            pkRows=[],
            fkRows=[],
        )
        tables = await _service(adapter).introspect(_datasource())
        assert len(tables) == 5


class TestSchemaVersion:
    def test_stable_for_same_data(self) -> None:
        a = [{"table_name": "A", "owner": "ZJTH", "columns": []}]
        assert schema_module._schemaVersion(a) == schema_module._schemaVersion(a)

    def test_changes_with_data(self) -> None:
        assert schema_module._schemaVersion([{"table_name": "A"}]) != schema_module._schemaVersion(
            [{"table_name": "B"}]
        )


class TestOwnerScoping:
    """Oracle owner 作用域化：显式 owner 内省 / 规范化 / 白名单 / listSchemas。"""

    async def test_introspect_uses_explicit_owner_in_sql(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {
                    "table_name": "DWD_M",
                    "column_name": "ID",
                    "data_type": "NUMBER",
                    "nullable": 0,
                    "owner": "THBI",
                }
            ],
            pkRows=[{"table_name": "DWD_M", "column_name": "ID"}],
            fkRows=[],
        )
        tables = await _service(adapter).introspect(_datasource(), owner="THBI")

        assert tables and tables[0].table_name == "DWD_M"
        assert tables[0].owner == "THBI"
        # SQL 内联 owner 为大写字面量 THBI；未回退到连接用户名 ZJTH
        assert any("owner = 'THBI'" in sql for sql in adapter.executed)
        assert not any("owner = 'ZJTH'" in sql for sql in adapter.executed)

    async def test_introspect_owner_lowercase_is_normalized(self) -> None:
        adapter = _FakeSchemaAdapter()
        await _service(adapter).introspect(_datasource(), owner="thbi")
        assert any("owner = 'THBI'" in sql for sql in adapter.executed)

    async def test_introspect_invalid_owner_raises_validation(self) -> None:
        with pytest.raises(ValidationError):
            await _service(_FakeSchemaAdapter()).introspect(_datasource(), owner="bad;owner")

    async def test_introspect_ignores_owner_for_pg_and_mysql(self) -> None:
        for type_ in ("postgresql", "mysql"):
            adapter = _FakeSchemaAdapter()
            # 显式 owner 在非 Oracle 下被忽略：仍走 information_schema，不抛错
            tables = await _service(adapter).introspect(_datasource(type_=type_), owner="THBI")
            assert tables == []
            assert adapter.executed  # 查询仍发生（information_schema 路径）

    async def test_list_schemas_returns_sorted_filtered_oracle_owners(self) -> None:
        adapter = _FakeSchemaAdapter(
            ownerRows=[
                {"owner": "thbi"},
                {"owner": "ZJTH"},
                {"owner": "SYS"},
                {"owner": "bad;drop"},
                {"owner": ""},
            ]
        )
        result = await _service(adapter).listSchemas(_datasource())
        assert result == ["SYS", "THBI", "ZJTH"]

    async def test_list_schemas_empty_for_pg_and_mysql(self) -> None:
        for type_ in ("postgresql", "mysql"):
            adapter = _FakeSchemaAdapter()
            result = await _service(adapter).listSchemas(_datasource(type_=type_))
            assert result == []
            assert adapter.executed == []  # 非 Oracle 不触发任何数据字典查询


class TestCommentEnrichment:
    """feat-ontology-import-comment：三数据源都把表/列 COMMENT 透传到 TableSchemaRead。

    验证要点：
    1. TableSchemaRead.comment 与 ColumnSchemaRead.comment 字段非 None
    2. _annotateComments 内部直接修改 merged（dict 引用透明）
    3. 无 comment 的表/列保持 None（向后兼容老数据源缓存）
    4. SQL 只读白名单：5 个新 SQL 都是只读 SELECT
    """

    async def test_oracle_introspect_includes_table_and_column_comments(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "PRECEIPT", "column_name": "PTHNUM_0", "data_type": "VARCHAR2", "nullable": 0, "owner": "ZJTH"},
                {"table_name": "PRECEIPT", "column_name": "BPSNUM_0", "data_type": "VARCHAR2", "nullable": 1, "owner": "ZJTH"},
            ],
            pkRows=[{"table_name": "PRECEIPT", "column_name": "PTHNUM_0"}],
            fkRows=[],
            tableCommentRows=[{"table_name": "PRECEIPT", "comment": "收货单主表"}],
            colCommentRows=[
                {"table_name": "PRECEIPT", "column_name": "PTHNUM_0", "comment": "收货单号"},
                {"table_name": "PRECEIPT", "column_name": "BPSNUM_0", "comment": "供应商编号"},
            ],
        )
        tables = await _service(adapter).introspect(_datasource())

        assert len(tables) == 1
        receipt = tables[0]
        assert receipt.comment == "收货单主表"
        assert receipt.columns[0].comment == "收货单号"
        assert receipt.columns[1].comment == "供应商编号"
        # 5 条 SQL：columns / pk / fk / table_comment / col_comment（Oracle owner 注入两次）
        assert any("ALL_TAB_COMMENTS" in sql for sql in adapter.executed)
        assert any("ALL_COL_COMMENTS" in sql for sql in adapter.executed)

    async def test_postgresql_introspect_includes_comments(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "orders", "column_name": "id", "data_type": "integer", "nullable": 0, "owner": "public"},
            ],
            pkRows=[{"table_name": "orders", "column_name": "id"}],
            fkRows=[],
            tableCommentRows=[{"table_name": "orders", "comment": "订单主表"}],
            colCommentRows=[{"table_name": "orders", "column_name": "id", "comment": "订单 ID"}],
        )
        tables = await _service(adapter).introspect(_datasource(type_="postgresql"))
        assert tables[0].comment == "订单主表"
        assert tables[0].columns[0].comment == "订单 ID"
        # PG 走 pg_description 视图，SQL 中必须出现
        assert any("pg_description" in sql for sql in adapter.executed)

    async def test_mysql_introspect_includes_comments(self) -> None:
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "wms_order", "column_name": "id", "data_type": "int", "nullable": 0, "owner": "wms"},
            ],
            pkRows=[{"table_name": "wms_order", "column_name": "id"}],
            fkRows=[],
            tableCommentRows=[{"table_name": "wms_order", "comment": "WMS 订单主表"}],
            colCommentRows=[{"table_name": "wms_order", "column_name": "id", "comment": "订单 PK"}],
        )
        tables = await _service(adapter).introspect(_datasource(type_="mysql"))
        assert tables[0].comment == "WMS 订单主表"
        assert tables[0].columns[0].comment == "订单 PK"
        # MySQL 列注释特征：information_schema.columns + column_comment
        assert any("column_comment" in sql for sql in adapter.executed)
        assert any("table_comment" in sql for sql in adapter.executed)

    async def test_no_comments_keeps_none_backward_compatible(self) -> None:
        """老数据源 / 新建无注释的表：TableSchemaRead.comment 与 ColumnSchemaRead.comment 均为 None。

        这是向后兼容的硬保证：老 schema_cache JSON 缺 comment 键时，Pydantic 默认 None。
        """
        adapter = _FakeSchemaAdapter(
            columnRows=[
                {"table_name": "plain_table", "column_name": "id", "data_type": "varchar", "nullable": 0, "owner": "public"},
            ],
            pkRows=[],
            fkRows=[],
            tableCommentRows=[],
            colCommentRows=[],
        )
        tables = await _service(adapter).introspect(_datasource(type_="postgresql"))
        assert tables[0].comment is None
        assert tables[0].columns[0].comment is None

    async def test_annotate_comments_helper_unit(self) -> None:
        """_annotateComments 静态函数：直接验证 key 大写归一 + 空串过滤 + dict 就地修改。"""
        from app.services.schema_introspection_service import _annotateComments

        merged = {
            "preceipt": {
                "owner": "ZJTH",
                "columns": [
                    {"column_name": "pthnum_0", "data_type": "VARCHAR2", "nullable": 0},
                ],
                "primary_keys": ["pthnum_0"],
                "foreign_keys": [],
            }
        }
        tableComments = [
            {"table_name": "PRECEIPT", "comment": "  收货单主表  "},  # 含空白 → strip 后保留
            {"table_name": "EMPTY_TBL", "comment": ""},  # 空串 → 忽略
            {"table_name": "WS_TBL", "comment": "   "},  # 纯空白 → 忽略
            {"table_name": "NULL_TBL", "comment": None},  # None → 忽略
        ]
        colComments = [
            {"table_name": "PRECEIPT", "column_name": "PTHNUM_0", "comment": "收货单号"},
            {"table_name": "PRECEIPT", "column_name": "BPSNUM_0", "comment": None},  # 无 col → 忽略
        ]
        _annotateComments(merged, tableComments, colComments)

        # 表注释 strip 后写入
        assert merged["preceipt"]["comment"] == "收货单主表"
        # 列注释（key 已大写归一）
        assert merged["preceipt"]["columns"][0]["comment"] == "收货单号"
        # 不在 _mergeRows 范围内的表/列不报错（静默忽略）
        assert "EMPTY_TBL" not in merged

    async def test_only_read_sql_executed_for_comment_queries(self) -> None:
        """所有 COMMENT 查询均为只读 SELECT（无 DML/DDL），SQL Guard 不会拦截。

        验证方式：执行过的 SQL 字面量不含 UPDATE/INSERT/DELETE/DROP/ALTER。
        """
        adapter = _FakeSchemaAdapter(
            columnRows=[{"table_name": "t", "column_name": "id", "data_type": "int", "nullable": 0, "owner": "p"}],
            pkRows=[{"table_name": "t", "column_name": "id"}],
            fkRows=[],
            tableCommentRows=[{"table_name": "t", "comment": "test"}],
            colCommentRows=[{"table_name": "t", "column_name": "id", "comment": "id"}],
        )
        await _service(adapter).introspect(_datasource(type_="postgresql"))
        for sql in adapter.executed:
            upper = sql.upper()
            for dml in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE "):
                assert dml not in upper, f"非只读 SQL 被执行: {sql!r}"
