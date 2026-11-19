"""本体表/列漂移交叉校验集成测试（2-4）。

真实 PG + 完整 API 链路验证：
- GET /api/v1/datasources/{id}/ontology-drift
  - 缓存存在且本体引用漂移 → hasDrift=true + missingTables/missingColumns
  - 缓存存在且干净 → hasDrift=false
  - 未缓存 → schemaCached=false（不臆测缺失，提示先 introspect）
  - 数据源不存在 → 404
- 对话链路：schema 漂移告警注入 NL2SQL 计划/SQL 的 system prompt（表漂移能告警）

说明：SchemaCache 用真实 PG 行（schema_data JSONB）；业务库适配器/LLM/Milvus 为外部依赖仍以 fake 注入。
"""

from __future__ import annotations

from decimal import Decimal

import app.api.v1.chat as chat_module
from sqlalchemy import select

from app.domain.models import (
    AuditLog,
    DataSource,
    LlmConfig,
    OntologyClass,
    OntologyProperty,
    SchemaCache,
)
from app.domain.schemas import ColumnSchemaRead, TableSchemaRead
from app.infrastructure.security.crypto import encryptApiKey
from app.services.schema_introspection_service import SchemaIntrospectionService, _schemaVersion

ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]


def _datasource() -> DataSource:
    return DataSource(
        name="ZJTH",
        type="oracle",
        host="h",
        port=1521,
        database_name="svc",
        username="ZJTH",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
        is_default=True,
    )


def _cacheSchema() -> list[dict]:
    """缓存中实际存在的表：PRECEIPT（NAME/QTY）、PRECEIPTD（仅 PTHNUM_0）。"""
    return [
        {
            "table_name": "PRECEIPT",
            "owner": "ZJTH",
            "columns": [
                {"column_name": "NAME", "data_type": "VARCHAR2", "nullable": False},
                {"column_name": "QTY", "data_type": "NUMBER", "nullable": True},
            ],
            "primary_keys": ["NAME"],
            "foreign_keys": [],
        },
        {
            "table_name": "PRECEIPTD",
            "owner": "ZJTH",
            "columns": [
                {"column_name": "PTHNUM_0", "data_type": "VARCHAR2", "nullable": False},
            ],
            "primary_keys": ["PTHNUM_0"],
            "foreign_keys": [],
        },
    ]


async def _persist(session, ds: DataSource) -> DataSource:
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    return ds


async def _seedDriftDataset(session) -> int:
    """写入数据源 + 缓存 + 三个本体类（其中两类漂移）；返回数据源 id。"""
    ds = await _persist(session, _datasource())
    schemaData = _cacheSchema()
    session.add(
        SchemaCache(
            datasource_id=ds.id,
            # Oracle 默认缓存行以 UPPER(username) 为键（_defaultSchemaName 语义）
            schema_name=ds.username,
            schema_data=schemaData,
            schema_version=_schemaVersion(schemaData),
        )
    )
    # 干净类：表与列都在缓存中
    session.add(
        OntologyClass(
            class_name="PRECEIPT",
            class_alias="收货单",
            source_table="ZJTH.PRECEIPT",
            properties=[
                OntologyProperty(property_name="NAME", data_type="STRING", source_column="NAME"),
                OntologyProperty(property_name="QTY", data_type="DECIMAL", source_column="QTY"),
            ],
        )
    )
    # 缺表：表不在缓存
    session.add(
        OntologyClass(
            class_name="OldReceipt",
            source_table="ZJTH.OLD_PRECEIPT",
            properties=[
                OntologyProperty(property_name="ID", data_type="DECIMAL", source_column="ID"),
            ],
        )
    )
    # 缺列：表在缓存但 GONE_COL 不存在
    session.add(
        OntologyClass(
            class_name="PRECEIPTD",
            source_table="ZJTH.PRECEIPTD",
            properties=[
                OntologyProperty(property_name="PTHNUM", data_type="STRING", source_column="PTHNUM_0"),
                OntologyProperty(property_name="GONE", data_type="STRING", source_column="GONE_COL"),
            ],
        )
    )
    await session.commit()
    return ds.id


class TestOntologyDriftApi:
    async def test_reports_missing_table_and_column(self, client, dbSession) -> None:
        dsId = await _seedDriftDataset(dbSession)

        resp = await client.get(f"/api/v1/datasources/{dsId}/ontology-drift")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["hasDrift"] is True
        assert body["schemaCached"] is True
        assert body["checkedTables"] == 3
        assert "ZJTH.OLD_PRECEIPT" in body["missingTables"]
        assert body["missingColumns"] == [{"table": "ZJTH.PRECEIPTD", "column": "GONE_COL"}]

    async def test_reports_clean_when_cache_matches(self, client, dbSession) -> None:
        ds = await _persist(dbSession, _datasource())
        schemaData = _cacheSchema()
        dbSession.add(
            SchemaCache(
                datasource_id=ds.id,
                schema_name=ds.username,
                schema_data=schemaData,
                schema_version=_schemaVersion(schemaData),
            )
        )
        # 只放干净类
        dbSession.add(
            OntologyClass(
                class_name="PRECEIPT",
                source_table="ZJTH.PRECEIPT",
                properties=[
                    OntologyProperty(property_name="NAME", data_type="STRING", source_column="NAME"),
                    OntologyProperty(property_name="QTY", data_type="DECIMAL", source_column="QTY"),
                ],
            )
        )
        await dbSession.commit()

        resp = await client.get(f"/api/v1/datasources/{ds.id}/ontology-drift")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["hasDrift"] is False
        assert body["schemaCached"] is True
        assert body["missingTables"] == []
        assert body["missingColumns"] == []
        assert body["checkedTables"] == 1

    async def test_without_cache_reports_no_drift(self, client, dbSession) -> None:
        ds = await _persist(dbSession, _datasource())
        resp = await client.get(f"/api/v1/datasources/{ds.id}/ontology-drift")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["schemaCached"] is False
        assert body["hasDrift"] is False
        assert body["checkedTables"] == 0

    async def test_missing_datasource_returns_404(self, client) -> None:
        resp = await client.get("/api/v1/datasources/9999/ontology-drift")
        assert resp.status_code == 404


class _PipelineLlm:
    """按 prompt 内容路由回复，记录每次调用的消息（与 test_chat_api 同构）。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = (
                '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"],'
                '"selectedProperties":["NAME","QTY"],"groupBy":["NAME"]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 2 条记录，各供应商收货量分布如下。"
        return _Resp()


class _FakeAdapter:
    async def execute_read_only(self, sql: str) -> list[dict]:
        return ROWS


class _RouterFor:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None


class _StubEmbeddingService:
    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None


class TestChatDriftWarning:
    async def test_drift_warning_injected_into_nl2sql_schema_prompt(
        self, client, dbSession, monkeypatch,
    ) -> None:
        """2-4：对话链路把漂移告警注入计划/SQL system prompt。

        口径（2026-11-19 数据源过滤上线后）：对话链路的类清单已按**当前数据源
        目录**（`schema_cache`）过滤过 —— 「表不在本数据源」的类**不再渲染进
        schema 文本**（结构性杜绝 ORA-00942，比「渲染 + 告警」更强）⇒
        计划/SQL prompt 里既没有这类表，也**不需要**对它告警；
        漂移告警在对话链路实际覆盖的是**列漂移**（表在、列没了）。

        缺表漂移仍在运维面可见：`GET /datasources/{id}/ontology-drift` 扫**全量**
        本体（不经数据源过滤），见本文件 `TestOntologyDriftApi`。
        """
        dsId = await _seedDriftDataset(dbSession)
        config = LlmConfig(
            model_name="test-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
        )
        dbSession.add(config)
        await dbSession.commit()
        await dbSession.refresh(config)

        llm = _PipelineLlm()
        monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
        monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
        monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
        monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())

        resp = await client.post(
            "/api/v1/chat",
            json={"sessionId": "s1", "question": "各供应商的收货数量汇总", "datasourceId": dsId},
        )
        assert resp.status_code == 200, resp.text

        # 计划阶段 system prompt 含 schema 文本与漂移告警（列漂移仍能告警）
        planSystem = llm.calls[0][0][1]
        assert "漂移" in planSystem
        assert "ZJTH.PRECEIPTD.GONE_COL" in planSystem
        # 「表不在当前数据源目录」的类（缺表/跨源）被**数据源过滤结构性剔除**
        # ⇒ prompt 里既没有该表，也不再对它告警（更强的保证：模型无从引用）。
        assert "ZJTH.OLD_PRECEIPT" not in planSystem

        # SQL 阶段 system prompt 同样携带告警
        sqlSystem = llm.calls[1][0][1]
        assert "漂移" in sqlSystem


# ---------------------------------------------------------------------------
# 本体发现 API（WP-ONT-AI A1-4）
#
# 与上面的漂移校验同属「本体 × 业务库 schema」这一面，故并入本文件
# （**不新建 integration 测试文件**：新文件会按「排序下标 mod 4」轮转 CI 分片
# 归属 ⇒ 假 net-new）。
#
# 业务库内省是外部依赖 ⇒ 以 fake 注入（与本文件既有口径一致：外部依赖 fake，
# 数据层真实 PG）；真实业务库采集由 `scripts/ontology_discovery_realdata.py` 覆盖。
# ---------------------------------------------------------------------------

_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
_USER_HEADERS = {"X-User-Id": "test-user", "X-User-Roles": "user"}


def _fakeTables() -> list[TableSchemaRead]:
    return [
        TableSchemaRead(
            table_name="PRECEIPT",
            owner="ZJTH",
            columns=[
                ColumnSchemaRead(column_name="NAME", data_type="VARCHAR2", nullable=False),
                ColumnSchemaRead(column_name="QTY", data_type="NUMBER", nullable=True),
            ],
            primary_keys=["NAME"],
            foreign_keys=[],
            comment="收货单",
        )
    ]


def _patchIntrospection(monkeypatch, tables: list[TableSchemaRead]) -> None:
    async def _fake(self, ds, *, owner=None):  # noqa: ANN001 - 测试替身
        return list(tables)

    monkeypatch.setattr(SchemaIntrospectionService, "introspect", _fake)


async def _registerSource(client, dbSession) -> tuple[int, int]:
    """登记一个发现源，返回 (sourceId, datasourceId)。"""

    ds = await _persist(dbSession, _datasource())
    resp = await client.post(
        "/api/v1/ontology-discovery/sources",
        json={
            "name": "ZJTH发现源",
            "datasource_id": ds.id,
            "owner": "张业务",
            "access_policy": {"mode": "READ_ONLY"},
        },
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], ds.id


class TestOntologyDiscoveryApi:
    async def test_登记发现源需要管理员(self, client, dbSession) -> None:
        ds = await _persist(dbSession, _datasource())

        resp = await client.post(
            "/api/v1/ontology-discovery/sources",
            json={"name": "越权源", "datasource_id": ds.id, "owner": "张业务"},
            headers=_USER_HEADERS,
        )

        assert resp.status_code == 403, resp.text

    async def test_登记发现源后可被普通登录用户列出(self, client, dbSession) -> None:
        sourceId, _ = await _registerSource(client, dbSession)

        resp = await client.get("/api/v1/ontology-discovery/sources")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1
        row = body["rows"][0]
        assert row["id"] == sourceId
        assert row["name"] == "ZJTH发现源"
        assert row["status"] == "ACTIVE"
        assert row["snapshotCount"] == 0

    async def test_采集快照写入快照行并可读详情(self, client, dbSession, monkeypatch) -> None:
        _patchIntrospection(monkeypatch, _fakeTables())
        sourceId, _ = await _registerSource(client, dbSession)

        resp = await client.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}",
            headers=_ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reused"] is False
        assert body["tableCount"] == 1
        assert body["columnCount"] == 2
        assert len(body["schemaHash"]) == 64

        listResp = await client.get(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}"
        )
        assert listResp.status_code == 200, listResp.text
        assert listResp.json()["total"] == 1

        detailResp = await client.get(
            f"/api/v1/ontology-discovery/snapshots/{body['snapshotId']}"
        )
        assert detailResp.status_code == 200, detailResp.text
        detail = detailResp.json()
        assert detail["schemaHash"] == body["schemaHash"]
        assert detail["snapshot"]["tables"][0]["tableName"] == "PRECEIPT"

    async def test_重复采集同内容复用既有快照(self, client, dbSession, monkeypatch) -> None:
        _patchIntrospection(monkeypatch, _fakeTables())
        sourceId, _ = await _registerSource(client, dbSession)

        first = await client.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}",
            headers=_ADMIN_HEADERS,
        )
        second = await client.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}",
            headers=_ADMIN_HEADERS,
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["reused"] is True
        assert second.json()["snapshotId"] == first.json()["snapshotId"]

        listResp = await client.get(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}"
        )
        assert listResp.json()["total"] == 1

    async def test_采集快照需要管理员(self, client, dbSession, monkeypatch) -> None:
        _patchIntrospection(monkeypatch, _fakeTables())
        sourceId, _ = await _registerSource(client, dbSession)

        resp = await client.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}",
            headers=_USER_HEADERS,
        )

        assert resp.status_code == 403, resp.text

    async def test_快照不存在返回404(self, client) -> None:
        resp = await client.get("/api/v1/ontology-discovery/snapshots/999999")

        assert resp.status_code == 404, resp.text

    async def test_差异端点返回基线与风险分级(self, client) -> None:
        resp = await client.get("/api/v1/ontology-discovery/diff")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "baselineReleaseId" in body
        assert body["hasBreakingChanges"] is False
        assert body["overallRiskLevel"] in {"LOW", "INFO"}
        for change in body["changes"]:
            assert change["riskLevel"] in {"HIGH", "LOW", "INFO"}
            assert change["severity"] in {"BREAKING", "ADDITIVE", "COSMETIC"}


class TestOntologyDiscoverySourceUpdate:
    """H-1：发现源必须有**停用 / 更新**路径，否则 `status=DISABLED` 是空承诺。"""

    async def test_更新发现源需要管理员(self, client, dbSession) -> None:
        sourceId, _ = await _registerSource(client, dbSession)

        resp = await client.patch(
            f"/api/v1/ontology-discovery/sources/{sourceId}",
            json={"status": "DISABLED"},
            headers=_USER_HEADERS,
        )

        assert resp.status_code == 403, resp.text

    async def test_管理员可停用发现源且停用后采集被拒(
        self, client, dbSession, monkeypatch
    ) -> None:
        _patchIntrospection(monkeypatch, _fakeTables())
        sourceId, _ = await _registerSource(client, dbSession)

        resp = await client.patch(
            f"/api/v1/ontology-discovery/sources/{sourceId}",
            json={"status": "DISABLED"},
            headers=_ADMIN_HEADERS,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "DISABLED"

        capture = await client.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}",
            headers=_ADMIN_HEADERS,
        )
        assert capture.status_code == 422, capture.text

    async def test_更新发现源不允许改数据源(self, client, dbSession) -> None:
        """`datasource_id` 是授权根：改它等于换一条被授权的连接 ⇒ 拒绝。"""

        sourceId, _ = await _registerSource(client, dbSession)

        resp = await client.patch(
            f"/api/v1/ontology-discovery/sources/{sourceId}",
            json={"datasourceId": 999},
            headers=_ADMIN_HEADERS,
        )

        assert resp.status_code == 422, resp.text

    async def test_访问策略含未知键被拒(self, client, dbSession) -> None:
        """M-1/M-2：未知策略键不静默忽略（否则「配了没生效」无人知晓）。"""

        ds = await _persist(dbSession, _datasource())

        resp = await client.post(
            "/api/v1/ontology-discovery/sources",
            json={
                "name": "未知键源",
                "datasource_id": ds.id,
                "owner": "张业务",
                "access_policy": {"mode": "READ_ONLY", "schemas": ["A"]},
            },
            headers=_ADMIN_HEADERS,
        )

        assert resp.status_code == 422, resp.text

    async def test_写端点落审计日志(self, client, dbSession, monkeypatch) -> None:
        """M-4：治理写动作必须留痕（登记 / 采集 / 停用）。"""

        _patchIntrospection(monkeypatch, _fakeTables())
        sourceId, _ = await _registerSource(client, dbSession)
        await client.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}",
            headers=_ADMIN_HEADERS,
        )
        await client.patch(
            f"/api/v1/ontology-discovery/sources/{sourceId}",
            json={"status": "DISABLED"},
            headers=_ADMIN_HEADERS,
        )

        rows = await dbSession.execute(
            select(AuditLog)
            .where(AuditLog.entity_type.in_(["discovery_source", "metadata_snapshot"]))
            .order_by(AuditLog.id)
        )
        actions = [(row.entity_type, row.action) for row in rows.scalars().all()]

        assert ("discovery_source", "CREATE") in actions
        assert ("metadata_snapshot", "CREATE") in actions
        assert ("discovery_source", "UPDATE") in actions
