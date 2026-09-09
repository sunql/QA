"""LocalImport API 集成测试（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 预览：200 返回 proposedClasses / proposedJoins；
- 执行：真实 PG 落库 class / property / join；
- 缺失数据源：404。

按项目规则，本模块走真实 PostgreSQL（integration/conftest.py 已强制覆盖根
client/dbSession fixtures），并从 HTTP 入口走完整 API 链路。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.domain.schemas import (
    ColumnSchemaRead,
    ForeignKeySchemaRead,
    SchemaIntrospectResponse,
    TableSchemaRead,
)
from app.services.local_import_service import LocalImportService


class _FakeSchemaService:
    """不连接业务库的 schema 源，返回固定两表（含外键）供导入。"""

    def __init__(self) -> None:
        self.introspectedOwners: list[str | None] = []

    async def introspectAndCache(self, session, ds, owner: str | None = None):
        self.introspectedOwners.append(owner)
        return None

    def buildResponse(self, cache):
        return SchemaIntrospectResponse(
            tables=[
                TableSchemaRead(
                    table_name="orders",
                    columns=[
                        ColumnSchemaRead(column_name="id", data_type="INT", nullable=False),
                        ColumnSchemaRead(
                            column_name="customer_id", data_type="INT", nullable=False
                        ),
                    ],
                    primary_keys=["id"],
                    foreign_keys=[
                        ForeignKeySchemaRead(
                            column_name="customer_id",
                            ref_table="customers",
                            ref_column="id",
                        )
                    ],
                ),
                TableSchemaRead(
                    table_name="customers",
                    columns=[
                        ColumnSchemaRead(column_name="id", data_type="INT", nullable=False)
                    ],
                    primary_keys=["id"],
                    foreign_keys=[],
                ),
            ],
            cached_at=datetime(2026, 8, 29, 0, 0, 0),
        )


def _useFakeSchema(client) -> LocalImportService:
    """把 schema 源替换为 fake，避免连接不可达的业务数据源；返回 service 供断言。"""
    service = LocalImportService(schema_service=_FakeSchemaService())
    client._transport.app.state.localImportService = service
    return service


def _create_payload() -> dict:
    return {
        "name": "test-import",
        "type": "postgresql",
        "host": "db.example.com",
        "port": 5432,
        "databaseName": "test",
        "username": "test",
        "password": "test",
    }


async def _create_datasource(client) -> int:
    resp = await client.post("/api/v1/datasources", json=_create_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_import_preview_returns_proposed_classes(client) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["datasourceId"] == ds_id
    assert len(body["proposedClasses"]) == 2
    assert {c["sourceTable"] for c in body["proposedClasses"]} == {"orders", "customers"}
    assert len(body["proposedJoins"]) == 1


async def test_import_execute_creates_class_property_join(client, dbSession) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    preview = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {}},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import",
        json={
            "confirmedClasses": body["proposedClasses"],
            "confirmedJoins": body["proposedJoins"],
            "conflictResolutions": [],
            "syncEmbeddings": False,
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["success"] is True
    assert result["createdClasses"] == 2
    assert result["createdProperties"] == 3
    assert result["createdJoins"] == 1

    classCount = await dbSession.scalar(select(func.count()).select_from(OntologyClass))
    propCount = await dbSession.scalar(select(func.count()).select_from(OntologyProperty))
    joinCount = await dbSession.scalar(select(func.count()).select_from(OntologyJoin))
    assert classCount == 2
    assert propCount == 3
    assert joinCount == 1


async def test_import_preview_missing_datasource_returns_404(client) -> None:
    _useFakeSchema(client)
    resp = await client.post(
        "/api/v1/datasources/99999/import-preview",
        json={"rules": {}},
    )
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]


async def test_import_execute_missing_datasource_returns_404(client) -> None:
    _useFakeSchema(client)
    resp = await client.post(
        "/api/v1/datasources/99999/import",
        json={
            "confirmedClasses": [],
            "confirmedJoins": [],
            "conflictResolutions": [],
            "syncEmbeddings": False,
        },
    )
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]


async def test_import_preview_applies_table_filter(client) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {"tableFilter": {"nameBlacklistPatterns": ["^cust"]}}},
    )
    assert resp.status_code == 200, resp.text
    tables = [c["sourceTable"] for c in resp.json()["proposedClasses"]]
    assert tables == ["orders"]  # customers 被 ^cust 过滤


async def test_import_preview_rejects_invalid_blacklist_regex(client) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    # 未闭合字符类 "[" 触发 re.error；应在 DTO 边界被拦截为 400，而非 500
    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {"tableFilter": {"nameBlacklistPatterns": ["["]}}},
    )
    assert resp.status_code == 400, resp.text
    assert "正则非法" in resp.json()["error"]


async def test_import_preview_applies_type_mapping(client) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {"typeMapping": {"mappings": {"INT": "DATETIME"}}}},
    )
    assert resp.status_code == 200, resp.text
    props = resp.json()["proposedClasses"][0]["properties"]
    assert all(p["dataType"] == "DATETIME" for p in props)


async def test_import_preview_detects_conflict_with_existing_class(client) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    # 先通过本体 API 创建一个 sourceTable=orders 的既有类
    created = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "OrdersExisting", "sourceTable": "orders"},
    )
    assert created.status_code == 201, created.text

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {}},
    )
    assert resp.status_code == 200, resp.text
    conflicts = resp.json()["conflicts"]
    assert any(c["type"] == "class" and c["sourceTable"] == "orders" for c in conflicts)


async def test_import_execute_respects_is_selected(client, dbSession) -> None:
    _useFakeSchema(client)
    ds_id = await _create_datasource(client)

    preview = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {}},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    classes = [
        {**c, "isSelected": False} if c["sourceTable"] == "customers" else c
        for c in body["proposedClasses"]
    ]

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import",
        json={
            "confirmedClasses": classes,
            "confirmedJoins": [],
            "conflictResolutions": [],
            "syncEmbeddings": False,
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["createdClasses"] == 1  # 仅 orders
    assert result["createdProperties"] == 2

    classCount = await dbSession.scalar(select(func.count()).select_from(OntologyClass))
    assert classCount == 1


async def test_import_preview_passes_schema_owner_to_introspection(client) -> None:
    """ImportPreviewRequest.schema（alias）透传到 introspectAndCache 的 owner。"""
    fake = _FakeSchemaService()
    client._transport.app.state.localImportService = LocalImportService(schema_service=fake)
    ds_id = await _create_datasource(client)

    resp = await client.post(
        f"/api/v1/datasources/{ds_id}/import-preview",
        json={"rules": {}, "schema": "THBI"},
    )
    assert resp.status_code == 200, resp.text
    assert fake.introspectedOwners == ["THBI"]
