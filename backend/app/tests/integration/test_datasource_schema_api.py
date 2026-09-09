"""数据源 Schema 自动发现 API 集成测试（5.7）。

验证 HTTP 契约：
- POST /api/v1/datasources/{id}/introspect → 200 camelCase 表清单 + cachedAt
- GET  /api/v1/datasources/{id}/schema     → 200（缓存版本）
- 未缓存时 GET schema → 404
- 数据源不存在 → 404
- 业务库不可达 → 400 错误包络
"""

from __future__ import annotations

from typing import Any

import app.api.v1.datasource as datasource_module
from app.services.schema_introspection_service import SchemaIntrospectionService

CREATE_PAYLOAD = {
    "name": "ZJTH-Oracle",
    "type": "oracle",
    "host": "192.168.205.70",
    "port": 1521,
    "databaseName": "X3V71ORA",
    "username": "ZJTH",
    "password": "secret",
    "description": "Sage X3 Oracle",
    "isActive": True,
    "isDefault": False,
}


class _FakeSchemaAdapter:
    """按 SQL 内容返回预置行集（覆盖 Oracle 三条数据字典查询 + ALL_TABLES）。"""

    def __init__(self, ownerRows: list[dict] | None = None) -> None:
        self.executed: list[str] = []
        self.ownerRows = ownerRows or []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "ALL_TABLES" in sql:
            return self.ownerRows
        if "ALL_TAB_COLUMNS" in sql:
            return [
                {"table_name": "PRECEIPT", "column_name": "PTHNUM_0", "data_type": "VARCHAR2", "nullable": 0, "owner": "ZJTH"},
                {"table_name": "PRECEIPT", "column_name": "TOTQTY_0", "data_type": "NUMBER", "nullable": 1, "owner": "ZJTH"},
                {"table_name": "PRECEIPTD", "column_name": "PTHNUM_0", "data_type": "VARCHAR2", "nullable": 0, "owner": "ZJTH"},
            ]
        if "constraint_type = 'P'" in sql:
            return [{"table_name": "PRECEIPT", "column_name": "PTHNUM_0"}]
        return [
            {
                "table_name": "PRECEIPTD",
                "column_name": "PTHNUM_0",
                "ref_table": "PRECEIPT",
                "ref_column": "PTHNUM_0",
            }
        ]


def _installFakeService(monkeypatch, adapter: Any) -> None:
    fakeService = SchemaIntrospectionService(adapterProvider=lambda datasourceId, ds: adapter)
    monkeypatch.setattr(datasource_module, "_schemaService", fakeService)


class TestSchemaApi:
    async def test_introspect_then_get_cached(self, client, monkeypatch) -> None:
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        assert resp.status_code == 201, resp.text
        dsId = resp.json()["id"]
        _installFakeService(monkeypatch, _FakeSchemaAdapter())

        resp = await client.post(f"/api/v1/datasources/{dsId}/introspect")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cachedAt"]
        assert [t["tableName"] for t in body["tables"]] == ["PRECEIPT", "PRECEIPTD"]

        receipt = body["tables"][0]
        assert receipt["owner"] == "ZJTH"
        assert receipt["primaryKeys"] == ["PTHNUM_0"]
        assert receipt["foreignKeys"] == []
        assert [(c["columnName"], c["dataType"], c["nullable"]) for c in receipt["columns"]] == [
            ("PTHNUM_0", "VARCHAR2", False),
            ("TOTQTY_0", "NUMBER", True),
        ]

        receiptd = body["tables"][1]
        assert receiptd["foreignKeys"][0]["columnName"] == "PTHNUM_0"
        assert receiptd["foreignKeys"][0]["refTable"] == "PRECEIPT"

        # 缓存版本 GET 返回一致内容
        resp2 = await client.get(f"/api/v1/datasources/{dsId}/schema")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["tables"][0]["tableName"] == "PRECEIPT"

    async def test_get_schema_without_cache_returns_404(self, client) -> None:
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = resp.json()["id"]

        resp = await client.get(f"/api/v1/datasources/{dsId}/schema")
        assert resp.status_code == 404
        assert "尚未缓存" in resp.json()["error"]

    async def test_introspect_missing_datasource_404(self, client) -> None:
        resp = await client.post("/api/v1/datasources/9999/introspect")
        assert resp.status_code == 404

    async def test_introspect_adapter_failure_returns_400(self, client, monkeypatch) -> None:
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = resp.json()["id"]

        class _BoomAdapter:
            async def execute_read_only(self, sql: str) -> list[dict]:
                raise ConnectionError("业务库不可达")

        _installFakeService(monkeypatch, _BoomAdapter())

        resp = await client.post(f"/api/v1/datasources/{dsId}/introspect")
        assert resp.status_code == 400
        assert "读取失败" in resp.json()["error"]
        # 底层连接异常文本不泄露到响应 detail
        assert "业务库不可达" not in resp.json()["detail"]
        assert "服务端日志" in resp.json()["detail"]


class TestSchemaOwnerScopingApi:
    """Oracle owner 作用域化 HTTP 契约：/schemas + ?schema 内省/读取隔离。"""

    PG_PAYLOAD = {
        "name": "PG-datasource",
        "type": "postgresql",
        "host": "db.example.com",
        "port": 5433,
        "databaseName": "db",
        "username": "pg",
        "password": "secret",
        "isActive": True,
        "isDefault": False,
    }

    async def test_schemas_endpoint_lists_oracle_owners(self, client, monkeypatch) -> None:
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = resp.json()["id"]
        adapter = _FakeSchemaAdapter(
            ownerRows=[
                {"owner": "THBI"},
                {"owner": "ZJTH"},
                {"owner": "bad;owner"},
                {"owner": "SYS"},
            ]
        )
        _installFakeService(monkeypatch, adapter)

        resp = await client.get(f"/api/v1/datasources/{dsId}/schemas")
        assert resp.status_code == 200, resp.text
        # 白名单过滤 + 大写 + 排序
        assert resp.json() == ["SYS", "THBI", "ZJTH"]

    async def test_schemas_endpoint_empty_for_postgres(self, client) -> None:
        resp = await client.post("/api/v1/datasources", json=self.PG_PAYLOAD)
        dsId = resp.json()["id"]

        resp = await client.get(f"/api/v1/datasources/{dsId}/schemas")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_introspect_and_get_scoped_by_schema_param(self, client, monkeypatch) -> None:
        """带 ?schema 的内省只写 THBI 缓存；默认 owner（ZJTH）仍 404 → 作用域隔离。"""
        resp = await client.post("/api/v1/datasources", json=CREATE_PAYLOAD)
        dsId = resp.json()["id"]
        adapter = _FakeSchemaAdapter()
        _installFakeService(monkeypatch, adapter)

        resp = await client.post(f"/api/v1/datasources/{dsId}/introspect?schema=THBI")
        assert resp.status_code == 200, resp.text
        assert any("owner = 'THBI'" in sql for sql in adapter.executed)
        assert not any("owner = 'ZJTH'" in sql for sql in adapter.executed)

        # 读同 schema 缓存命中
        got = await client.get(f"/api/v1/datasources/{dsId}/schema?schema=THBI")
        assert got.status_code == 200, got.text

        # 默认 owner（ZJTH）未内省 → 404（schema 作用域隔离生效）
        miss = await client.get(f"/api/v1/datasources/{dsId}/schema")
        assert miss.status_code == 404

    async def test_schemas_endpoint_missing_datasource_404(self, client) -> None:
        resp = await client.get("/api/v1/datasources/9999/schemas")
        assert resp.status_code == 404
