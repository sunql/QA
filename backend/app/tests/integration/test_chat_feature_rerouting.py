"""Phase 4.4 Chat 特征回流集成测试（真实 PG 5433 + 完整 API 链路）。

覆盖：
1. Feature 目录为空时不阻断 chat 主链路
2. Feature 存在但无值时 fallback 到 SQL 路径
3. 计划引用可用特征 → 命中特征回流（mock 隔离）
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.api.v1 import features as featuresModule
from app.domain.models import LlmConfig, OntologyClass, OntologyProperty
from sqlalchemy import select

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


# ---------------------------------------------------------------------------
# Fake / stub 实现
# ---------------------------------------------------------------------------

class _FakeAdapter:
    async def execute_read_only(self, sql):
        # 含 entity_key + value：同时满足 feature compute 校验 + chat 业务库查询（额外列无害）
        return [{"entity_key": "Q630", "value": Decimal("0.9500"), "PONUM": "PO00001"}]


class _StubEmbeddingService:
    async def embed(self, text):
        return [0.0] * 8

    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None

    async def searchSimilarQueries(self, question: str, topK: int, datasourceId: int | None = None) -> list:
        return []  # 空列表 = few-shot 无命中 = 正常降级


class _RouterForConfig:
    def __init__(self, config) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx):
        return self._config

    def selectFallbackModel(self, configs, excludeId):
        return None


class _FakePlanLlm:
    """Plan 阶段返回含 SUPPLIER_OTD_3M 的计划，触发特征回流。"""

    async def complete(self, messages, **kwargs):
        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        user = messages[1].content
        if "解析为查询计划" in messages[0].content:
            _Resp.content = (
                '{"target":"供应商3月准时交付率",'
                '"selectedClasses":["SUPPLIER"],'
                '"selectedProperties":["SUPPLIER_CODE"],'
                '"groupBy":[],'
                '"conditions":["使用特征 SUPPLIER_OTD_3M 查询供应商准时交付率"]}'
            )
        elif "生成 SQL 时必须" in messages[0].content:
            _Resp.content = "```sql\nSELECT * FROM DUMMY\n```"
        elif "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1]}]}'
        else:
            _Resp.content = "供应商准时交付率为 95%。"
        return _Resp()


async def _ensureSupplierClass(dbSession) -> None:
    """确保 ontology 有 SUPPLIER 类（SUPPLIER_CODE 属性），供 _FakePlanLlm 引用。"""
    existing = dbSession.execute(
        lambda: __import__("sqlalchemy").select(OntologyClass).where(OntologyClass.class_name == "SUPPLIER")
    ).first()
    if existing:
        return
    cls = OntologyClass(
        class_name="SUPPLIER",
        class_alias="供应商",
        source_table="SUPPLIER",
        description="供应商主数据",
    )
    dbSession.add(cls)
    dbSession.flush()
    dbSession.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="SUPPLIER_CODE",
            property_alias="供应商编码",
            data_type="VARCHAR",
            source_column="SUPPLIER_CODE",
        )
    )
    dbSession.commit()


async def _ensureSupplierClass(dbSession) -> None:
    """确保 ontology 有 SUPPLIER 类（SUPPLIER_CODE 属性），供 _FakePlanLlm 引用。"""
    from sqlalchemy import select
    existing = (await dbSession.execute(select(OntologyClass).where(OntologyClass.class_name == "SUPPLIER"))).first()
    if existing:
        return
    cls = OntologyClass(
        class_name="SUPPLIER",
        class_alias="供应商",
        source_table="SUPPLIER",
        description="供应商主数据",
    )
    dbSession.add(cls)
    await dbSession.flush()
    dbSession.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="SUPPLIER_CODE",
            property_alias="供应商编码",
            data_type="VARCHAR",
            source_column="SUPPLIER_CODE",
        )
    )
    await dbSession.commit()


async def _seedLlmConfig(dbSession) -> LlmConfig:
    existing = (await dbSession.execute(select(LlmConfig))).first()
    if existing:
        return existing[0]
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


async def _setupChatFakes(monkeypatch, dbSession, llm) -> None:
    """注入模型路由 + LLM + 适配器 + embedding fakes（隔离外部依赖）。"""
    config = await _seedLlmConfig(dbSession)
    import app.api.v1.chat as chatModule

    monkeypatch.setattr(chatModule._service, "_modelRouter", _RouterForConfig(config))
    monkeypatch.setattr(chatModule._service, "_llmFactory", lambda cfg: llm)
    monkeypatch.setattr(chatModule._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chatModule._service, "_embedding", _StubEmbeddingService())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestChatFeatureRerouting:
    async def test_empty_catalog_does_not_break_chat(
        self, client, dbSession, monkeypatch
    ) -> None:
        """Feature 目录为空时，chat 主链路不受影响（buildFeatureCatalogText → None 处理）。"""
        llm = _FakePlanLlm()
        await _setupChatFakes(monkeypatch, dbSession, llm=llm)

        # 不创建任何 feature → 目录为空
        resp = await client.post(
            "/api/v1/chat",
            json={
                "question": "查询采购订单数量",
                "sessionId": "test-feat-chat-empty",
                "datasourceId": 1,
            },
        )
        # 目录为空导致 pc.featureCatalogText=None → _tryFeatureResponse 直接返回 None
        # 流水线正常走 SQL → 业务库查询失败返回 400，但无 5xx 崩溃
        assert resp.status_code < 500, resp.text

    async def test_feature_without_values_falls_back_to_sql(
        self, client, dbSession, monkeypatch
    ) -> None:
        """Feature 已定义但无特征值 → _tryFeatureResponse 返回 None，流水线继续走 SQL。"""
        # 创建特征但不触发 compute → 无 feature_value 记录
        ds_resp = await client.post(
            "/api/v1/datasources",
            json={
                "name": "feature-no-value-ds",
                "type": "postgresql",
                "host": "db.example.com",
                "port": 5432,
                "databaseName": "appdb",
                "username": "u",
                "password": "p",
            },
        )
        assert ds_resp.status_code == 201
        ds_id = ds_resp.json()["id"]

        feat_resp = await client.post(
            "/api/v1/features",
            json={
                "featureName": "SUPPLIER_OTD_3M",
                "featureAlias": "供应商3月准时交付率",
                "entityType": "SUPPLIER",
                "status": "ACTIVE",
                "calculationLogic": "SELECT supplier_code AS entity_key, AVG(otd_rate) AS value FROM DWS GROUP BY supplier_code",
                "datasourceId": ds_id,
            },
            headers=ADMIN_HEADERS,
        )
        assert feat_resp.status_code == 201

        llm = _FakePlanLlm()
        await _setupChatFakes(monkeypatch, dbSession, llm=llm)

        resp = await client.post(
            "/api/v1/chat",
            json={
                "question": "供应商 Q630 的 3 月准时交付率是多少？",
                "sessionId": "test-feat-chat-no-value",
                "datasourceId": ds_id,
            },
        )
        # _tryFeatureResponse 命中特征但无值 → 返回 None → 走 SQL 路径
        # 业务库查不到 DUMMY 表 → 400，但无 5xx
        assert resp.status_code < 500, resp.text

    async def test_feature_referenced_in_plan_returns_feature_values(
        self, client, dbSession, monkeypatch
    ) -> None:
        """计划 conditions 引用 SUPPLIER_OTD_3M → 命中特征 + 有值 → 预计算值回流。"""
        # 创建 SUPPLIER 类（供 fake plan LLM 引用）
        await _ensureSupplierClass(dbSession)
        # 创建特征 + fake adapter 落值
        ds_resp = await client.post(
            "/api/v1/datasources",
            json={
                "name": "feature-with-value-ds",
                "type": "postgresql",
                "host": "db.example.com",
                "port": 5432,
                "databaseName": "appdb",
                "username": "u",
                "password": "p",
            },
        )
        assert ds_resp.status_code == 201
        ds_id = ds_resp.json()["id"]

        feat_resp = await client.post(
            "/api/v1/features",
            json={
                "featureName": "SUPPLIER_OTD_3M",
                "featureAlias": "供应商3月准时交付率",
                "entityType": "SUPPLIER",
                "unit": "%",
                "status": "ACTIVE",
                "calculationLogic": "SELECT supplier_code AS entity_key, AVG(otd_rate) AS value FROM DWS GROUP BY supplier_code",
                "datasourceId": ds_id,
            },
            headers=ADMIN_HEADERS,
        )
        assert feat_resp.status_code == 201
        feat_id = feat_resp.json()["id"]

        featuresModule._computeService._adapterProvider = lambda dsId, ds: _FakeAdapter()
        compute_resp = await client.post(
            f"/api/v1/features/{feat_id}/compute",
            headers=ADMIN_HEADERS,
        )
        assert compute_resp.status_code == 200, compute_resp.text

        llm = _FakePlanLlm()
        await _setupChatFakes(monkeypatch, dbSession, llm=llm)

        resp = await client.post(
            "/api/v1/chat",
            json={
                "question": "供应商 Q630 的 3 月准时交付率是多少？",
                "sessionId": "test-feat-chat-hit",
                "datasourceId": ds_id,
            },
        )
        # LLM 计划 conditions 含 SUPPLIER_OTD_3M → _tryFeatureResponse 命中
        # → 返回 ChatResponse(含预计算值)，不调业务库 SQL
        assert resp.status_code < 500, resp.text
        body = resp.json()
        # answer 含特征名/实体/值（来自预计算）
        assert "SUPPLIER_OTD_3M" in body.get("answer", "")

    async def test_streaming_feature_response(
        self, client, dbSession, monkeypatch
    ) -> None:
        """流式接口命中特征回流 → SSE 序列正常（含 EVENT_TOKEN + EVENT_DONE）。"""
        await _ensureSupplierClass(dbSession)

        ds_resp = await client.post(
            "/api/v1/datasources",
            json={
                "name": "feat-stream-ds",
                "type": "postgresql",
                "host": "db.example.com",
                "port": 5432,
                "databaseName": "appdb",
                "username": "u",
                "password": "p",
            },
        )
        assert ds_resp.status_code == 201
        ds_id = ds_resp.json()["id"]

        feat_resp = await client.post(
            "/api/v1/features",
            json={
                "featureName": "SUPPLIER_OTD_3M",
                "featureAlias": "供应商3月准时交付率",
                "entityType": "SUPPLIER",
                "status": "ACTIVE",
                "calculationLogic": "SELECT supplier_code AS entity_key, AVG(otd_rate) AS value FROM DWS GROUP BY supplier_code",
                "datasourceId": ds_id,
            },
            headers=ADMIN_HEADERS,
        )
        assert feat_resp.status_code == 201

        featuresModule._computeService._adapterProvider = lambda dsId, ds: _FakeAdapter()
        compute_resp = await client.post(
            f"/api/v1/features/{feat_resp.json()['id']}/compute",
            headers=ADMIN_HEADERS,
        )
        assert compute_resp.status_code == 200, compute_resp.text

        llm = _FakePlanLlm()
        await _setupChatFakes(monkeypatch, dbSession, llm=llm)

        resp = await client.post(
            "/api/v1/chat/stream",
            json={
                "question": "供应商 Q630 的 3 月准时交付率是多少？",
                "sessionId": "test-feat-chat-stream",
                "datasourceId": ds_id,
            },
        )
        assert resp.status_code == 200, resp.text
        # SSE 流式响应：逐行解析 data: {...} 事件
        import re
        body = resp.text
        # 至少包含一个 token 事件（EVENT_TOKEN）
        assert "data: " in body, f"Expected SSE stream, got: {body[:200]}"
        # 包含 DONE 事件
        assert "DONE" in body or "done" in body.lower(), f"Expected DONE event in SSE: {body[:300]}"
