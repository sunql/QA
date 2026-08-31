"""Chat 集成 supplier-360 端到端测试（Phase 5.3）。

端到端覆盖 chat_service.processMessage 拦截 SUPPLIER_360 意图后：
- 完整路径：seed supplier + features → 问"供应商 X 的 360° 视图"→ ChatResponse 含 supplier360
- supplierKey 缺失：问"供应商全貌"无 key → ChatResponse answer 引导 + supplier360=None
- supplierKey 找不到：问"供应商 999999 的 360 视图"→ ChatResponse answer=NotFound 消息
- 普通查询不拦截：问"供应商 100001 的订单数"→ 走 QUERY 路径（NL2SQL）
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    EntityType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue, LlmConfig

# 默认 stub user 带 admin（参见 dependencies.DEFAULT_STUB_ROLES）
AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    """seed 一个最小可用 DataSource，chat 请求必填 datasourceId。"""
    ds = DataSource(
        id=9201,
        name="ds-chat-360",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds


async def _seedSupplier(dbSession: AsyncSession, key: int, code: str) -> None:
    dbSession.add(
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        )
    )
    await dbSession.commit()


async def _seedFeatureAndValue(
    dbSession: AsyncSession,
    feature_id: int,
    feature_name: str,
    code: str,
    value: float,
) -> None:
    dbSession.add(
        FeatureDefinition(
            id=feature_id,
            feature_name=feature_name,
            feature_alias=feature_name,
            feature_definition="auto",
            entity_type=EntityType.SUPPLIER,
            calculation_logic="SELECT 1",
            window_size="3M",
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit="%",
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=9201,
            version="v1.0",
        )
    )
    dbSession.add(
        FeatureValue(
            feature_id=feature_id,
            entity_key=code,
            value=value,
            valid_at=date(2026, 8, 31),
            computed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )
    await dbSession.commit()


# ---------------------------------------------------------------------------
# Chat fakes（沿用 test_chat_data_quality_integration.py 模式；本测试只需 200）
# ---------------------------------------------------------------------------


class _StubEmbedding:
    async def embed(self, text):  # noqa: ARG002
        return [0.0] * 8

    async def storeQueryEmbedding(self, **kwargs):  # noqa: ARG002
        return None

    async def searchSimilarQueries(self, *args, **kwargs):  # noqa: ARG002
        return []


class _FakeAdapter:
    async def execute_read_only(self, sql):  # noqa: ARG002
        return []


class _NoopLlm:
    """返回最小可用响应即可，本测试不校验 NL2SQL 内容。

    沿用 test_chat_data_quality_integration._makePipelineLlm 思路：按 prompt 阶段
    返回 plan / sql / chart / answer，让 NL2SQL 解析链路走完。
    """

    async def complete(self, messages, **kwargs):  # noqa: ARG002
        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 1
            completionTokens = 1

        user = messages[1].content
        system = messages[0].content
        if "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = (
                '{"target":"x","selectedClasses":[],'
                '"selectedProperties":[],"groupBy":[]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = "```sql\nSELECT 1 AS c\n```"
        else:
            _Resp.content = "ok"
        return _Resp()


class _RouterForConfig:
    def __init__(self, config) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx):  # noqa: ARG002
        return self._config

    def selectFallbackModel(self, configs, excludeId):  # noqa: ARG002
        return None


async def _setupChatFakes(monkeypatch, dbSession: AsyncSession) -> None:
    """注入 chat 必需依赖，让 NL2SQL 路径不报「没有可用的模型配置」。"""
    existing = (await dbSession.execute(select(LlmConfig))).first()
    if existing:
        config = existing[0]
    else:
        config = LlmConfig(
            model_name="test-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.001"),
            cost_per_1k_output=Decimal("0.002"),
        )
        dbSession.add(config)
        await dbSession.commit()
        await dbSession.refresh(config)
    import app.api.v1.chat as chatModule

    monkeypatch.setattr(chatModule._service, "_modelRouter", _RouterForConfig(config))
    monkeypatch.setattr(chatModule._service, "_llmFactory", lambda cfg: _NoopLlm())
    monkeypatch.setattr(chatModule._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chatModule._service, "_embedding", _StubEmbedding())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_supplier_360_intent_returns_payload(
    client: AsyncClient, dbSession: AsyncSession
):
    """完整路径：seed supplier + feature → chat 问"供应商 100001 的 360" → 含 supplier360。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 92.5
    )

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-s1",
            "question": "供应商 100001 的 360° 视图",
            "datasourceId": 9201,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    # answer 含 enterprise_code + 关键聚合字段
    assert "SUP000001" in body["answer"]
    assert "100001" in body["answer"]
    # supplier360 字段（camelCase via alias_generator）含完整数据
    payload = body["supplier360"]
    assert payload is not None
    assert payload["profile"]["enterpriseKey"] == 100001
    assert payload["profile"]["enterpriseCode"] == "SUP000001"
    assert len(payload["entityCodes"]) == 1
    assert payload["entityCodes"][0]["sourceSystem"] == "ERP"
    # OTD 已 seed → latest=True
    otd = next(
        k for k in payload["kpis"] if k["featureName"] == "SUPPLIER_OTD_3M"
    )
    assert otd["latest"] is True


@pytest.mark.asyncio
async def test_chat_supplier_360_missing_key_returns_guidance(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
):
    """supplierKey 缺失 → 不走 supplier_360 拦截，supplier360 必为 None。"""
    await _seedDatasource(dbSession)
    await _setupChatFakes(monkeypatch, dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-s2",
            "question": "供应商 360 度全景",
            "datasourceId": 9201,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("supplier360") is None
    # 不强制 intent（避免过度拦截普通查询）
    assert body["intent"] != "supplier_360"


@pytest.mark.asyncio
async def test_chat_supplier_360_not_found_returns_notfound_message(
    client: AsyncClient, dbSession: AsyncSession
):
    """supplierKey 有但 entity_mapping 无 → 200 + answer=NotFound 通用消息 + supplier360=None。"""
    await _seedDatasource(dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-s3",
            "question": "供应商 999999 的 360 视图",
            "datasourceId": 9201,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    assert body["supplier360"] is None
    assert "999999" in body["answer"]
    # 与 ACL 4.5 原则一致：通用消息，不泄漏「不存在 vs 无权限」细节
    assert "不存在" in body["answer"] or "建档" in body["answer"]


@pytest.mark.asyncio
async def test_chat_normal_query_not_intercepted_by_supplier_360(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
):
    """普通供应商查询不进入 supplier-360 拦截路径（防止误吸）。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-s4",
            "question": "供应商 100001 的订单数",
            "datasourceId": 9201,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 不应被吸到 supplier_360
    assert body["intent"] != "supplier_360"
    assert body.get("supplier360") is None
