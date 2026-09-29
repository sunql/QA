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


# =============================================================================
# 批量导入完成后自动补齐类向量（整批对账，单次 flush）
#
# 背景：导入 N 个类若走逐类后台同步（createClass 默认路径），N 个任务并发
# flush（单次 8-25s）会拖垮 Milvus；改为导入完成后一次性 syncMissing。
# =============================================================================


def _waitFor(check, timeout: float = 10.0):
    """后台任务轮询等待（与 test_ontology_api 同款）。"""
    import asyncio
    import time

    async def _run() -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if check():
                return True
            await asyncio.sleep(0.1)
        return check()

    return _run()


async def test_import_execute_batches_embedding_sync(client, dbSession) -> None:
    import app.api.v1.ontology as ontology_api
    import app.services.ontology_service as ontology_module
    from app.services.ontology_service import OntologyService

    texts: list[str] = []

    async def fakeGenerateEmbedding(text: str) -> list[float]:
        texts.append(text)
        return [0.1] * 1024

    monkey_target = ontology_api._embeddingService
    monkey_target.generateEmbedding = fakeGenerateEmbedding

    insertCalls: list[list[dict]] = []
    ontology_module.milvus.insertEmbeddings = lambda rows: insertCalls.append(rows)
    ontology_module.milvus.listAllEmbeddings = lambda: []

    # 注入共享 embedding service 的 OntologyService，避免测试连真实 LLM
    service = LocalImportService(
        schema_service=_FakeSchemaService(),
        ontology_service=OntologyService(embeddingService=monkey_target),
    )
    client._transport.app.state.localImportService = service
    ds_id = await _create_datasource(client)

    try:
        preview = await client.post(
            f"/api/v1/datasources/{ds_id}/import-preview",
            json={"rules": {}},
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()

        # 不传 syncEmbeddings → 默认 True（自动补齐）
        resp = await client.post(
            f"/api/v1/datasources/{ds_id}/import",
            json={
                "confirmedClasses": body["proposedClasses"],
                "confirmedJoins": body["proposedJoins"],
                "conflictResolutions": [],
            },
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["success"] is True
        assert result["createdClasses"] == 2

        # 整批补齐在后台落地：2 个类 + 全部属性向量、恰好一次整批 insert（单次 flush）。
        # 2026-09-18 起对账扩展为类+属性双覆盖（此前只补类，属性缺口只能靠 backfill 脚本）。
        def rows() -> list[dict]:
            return [r for call in insertCalls for r in call]

        assert await _waitFor(lambda: len(rows()) == 5), (
            f"导入后应整批补齐 2 类 + 3 属性共 5 条向量，实际 {len(rows())}"
        )
        assert len(insertCalls) == 1, "5 条向量应合并为一次整批 insert"
        assert {r["type"] for r in rows()} == {"class", "property"}
        assert sum(1 for r in rows() if r["type"] == "class") == 2
        assert sum(1 for r in rows() if r["type"] == "property") == 3
        assert len(texts) == 5
    finally:
        del monkey_target.generateEmbedding
