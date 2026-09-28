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
from app.domain.models import DataSource, LlmConfig, OntologyClass, OntologyProperty, SchemaCache
from app.infrastructure.security.crypto import encryptApiKey
from app.services.schema_introspection_service import _schemaVersion

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
        """2-4：对话链路把漂移告警注入计划/SQL system prompt（表漂移能告警）。"""
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

        # 计划阶段 system prompt 含 schema 文本与漂移告警（缺表 + 缺列）
        planSystem = llm.calls[0][0][1]
        assert "漂移" in planSystem
        assert "ZJTH.OLD_PRECEIPT" in planSystem
        assert "ZJTH.PRECEIPTD.GONE_COL" in planSystem

        # SQL 阶段 system prompt 同样携带告警
        sqlSystem = llm.calls[1][0][1]
        assert "漂移" in sqlSystem
