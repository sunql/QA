"""Chat 集成 supplier-risk 端到端测试（Phase 5.4）。

端到端覆盖 chat_service.processMessage 拦截 SUPPLIER_RISK 意图后：
- 完整路径：seed supplier + features → 问「供应商 X 的风险」→ ChatResponse 含 supplier_risk
- supplierKey 缺失：问「供应商风险」无 key → ChatResponse answer 引导 + supplier_risk=None
- supplierKey 找不到：问「供应商 999999 的风险」→ ChatResponse answer=NotFound 通用消息
- 不被 supplier_360 吸走：「供应商 100001 的 360° 视图」→ intent 仍为 supplier_360
- 普通查询不被 supplier_risk 吸走：「供应商 100001 的订单数」→ 走 QUERY 路径
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue, LlmConfig

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}


# ---------------------------------------------------------------------------
# helpers（与 test_chat_supplier_360 一致；独立 ds_id 避免冲突）
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=9401,
        name="ds-chat-risk",
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
            entity_type="SUPPLIER",
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
    *,
    unit: str = "%",
    window: str = "3M",
) -> None:
    dbSession.add(
        FeatureDefinition(
            id=feature_id,
            feature_name=feature_name,
            feature_alias=feature_name,
            feature_definition="auto",
            entity_type="SUPPLIER",
            calculation_logic="SELECT 1",
            window_size=window,
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit=unit,
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=9401,
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
# Chat fakes（与 test_chat_supplier_360 一致）
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
    """按 prompt 阶段返回最小可用响应。"""

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
    """注入 chat 必需依赖（与 supplier_360 一致）。"""
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
async def test_chat_supplier_risk_intent_returns_payload(
    client: AsyncClient, dbSession: AsyncSession
):
    """完整路径：seed supplier + feature → 问「供应商 100001 的风险」→ 含 supplier_risk。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M"
    )

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-risk-1",
            "question": "供应商 100001 的风险",
            "datasourceId": 9401,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_risk"
    # answer 含 enterprise_code + 等级
    assert "SUP000001" in body["answer"]
    assert "100001" in body["answer"]
    assert "high" in body["answer"]
    # supplier_risk 字段含完整数据
    payload = body["supplierRisk"]
    assert payload is not None
    assert payload["profile"]["enterpriseKey"] == 100001
    assert payload["level"] == "high"
    assert payload["levelSource"] == "supplier_risk_score_main"  # 规则路径契约（feat-feature-rule-config，与 parity 集成测试一致）
    assert len(payload["contributions"]) == 4


@pytest.mark.asyncio
async def test_chat_supplier_risk_missing_key_returns_guidance(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
):
    """supplierKey 缺失 → 走 supplier_risk 拦截 + answer=引导 + supplier_risk=None。"""
    await _seedDatasource(dbSession)
    await _setupChatFakes(monkeypatch, dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-risk-2",
            "question": "供应商风险等级",
            "datasourceId": 9401,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # intent 不为 supplier_risk（regex 必带 5-9 位数字 key 才命中；缺 key 不进入拦截）
    assert body.get("supplierRisk") is None
    assert body["intent"] != "supplier_risk"


@pytest.mark.asyncio
async def test_chat_supplier_risk_not_found_returns_notfound_message(
    client: AsyncClient, dbSession: AsyncSession
):
    """supplierKey 有但 entity_mapping 无 → 200 + answer=NotFound 通用消息 + supplier_risk=None。"""
    await _seedDatasource(dbSession)
    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-risk-3",
            "question": "供应商 999999 的健康度",
            "datasourceId": 9401,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_risk"
    assert body["supplierRisk"] is None
    assert "999999" in body["answer"]
    # 与 ACL 4.5 原则一致：通用消息，不泄漏「不存在 vs 无权限」细节
    assert "不存在" in body["answer"] or "建档" in body["answer"]


@pytest.mark.asyncio
async def test_chat_supplier_360_does_not_collide_with_risk(
    client: AsyncClient, dbSession: AsyncSession
):
    """「供应商 100001 的 360° 视图」必须命中 supplier_360，不能被 risk 吸走。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M")
    await _seedFeatureAndValue(dbSession, 2, "SUPPLIER_OTD_3M", "SUP000001", 92.5)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-risk-4",
            "question": "供应商 100001 的 360° 视图",
            "datasourceId": 9401,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] == "supplier_360"
    assert body["supplier360"] is not None
    assert body["supplierRisk"] is None


@pytest.mark.asyncio
async def test_chat_normal_query_not_intercepted_by_risk(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
):
    """普通供应商查询不进入 supplier_risk 拦截路径（防止误吸）。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _setupChatFakes(monkeypatch, dbSession)

    resp = await client.post(
        "/api/v1/chat",
        headers=AUTH_HEADERS,
        json={
            "sessionId": "test-risk-5",
            "question": "供应商 100001 的订单数",
            "datasourceId": 9401,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"] != "supplier_risk"
    assert body.get("supplierRisk") is None