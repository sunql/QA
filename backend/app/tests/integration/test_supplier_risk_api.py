"""Supplier Risk Agent REST API 集成测试（Phase 5.4）。

真实 PG 5433 + 完整 HTTP 链路。
覆盖：
- GET /api/v1/supplier-risk/{key}：seed 完整 4 feature → 200 + SupplierRiskRead
- supplier 不在 entity_mapping → 404（防 typo 静默）
- 不注入 LLM → 200 + risk_points_source=fallback_template（异常隔离）

ACL：与 supplier_360 一致，仅 getCurrentUser。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> None:
    dbSession.add(
        DataSource(
            id=9301,
            name="ds-risk-api",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
        )
    )
    await dbSession.commit()


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
            datasource_id=9301,
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_supplier_risk_returns_full_payload(
    client: AsyncClient, dbSession: AsyncSession
):
    """seed 完整 4 feature → 200 + SupplierRiskRead 含 level + risk_points + actions。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M"
    )
    await _seedFeatureAndValue(dbSession, 2, "SUPPLIER_OTD_3M", "SUP000001", 85.0)
    await _seedFeatureAndValue(dbSession, 3, "SUPPLIER_DEFECT_RATE_3M", "SUP000001", 6.0)
    await _seedFeatureAndValue(dbSession, 4, "SUPPLIER_PRICE_VARIANCE_3M", "SUP000001", 8.0)

    resp = await client.get(
        "/api/v1/supplier-risk/100001", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 主路径 RISK_SCORE=0.50 < 0.60 → High
    assert body["level"] == "high"
    assert body["levelSource"] == "risk_score"
    # contributions 4 个
    assert len(body["contributions"]) == 4
    names = {c["featureName"] for c in body["contributions"]}
    assert names == {
        "SUPPLIER_OTD_3M",
        "SUPPLIER_DEFECT_RATE_3M",
        "SUPPLIER_PRICE_VARIANCE_3M",
        "SUPPLIER_RISK_SCORE",
    }
    # 未注入 LLM → fallback_template + tokens_used=0 + modelName=null
    assert body["riskPointsSource"] == "fallback_template"
    assert body["tokensUsed"] == 0
    assert body["cost"] == 0.0
    assert body["llmModelName"] is None
    # recommended_actions 非空（High 4 条）
    assert len(body["recommendedActions"]) >= 1
    # profile 复用 supplier_360
    assert body["profile"]["enterpriseKey"] == 100001
    assert body["profile"]["enterpriseCode"] == "SUP000001"


@pytest.mark.asyncio
async def test_get_supplier_risk_404_when_supplier_not_in_mapping(
    client: AsyncClient, dbSession: AsyncSession
):
    """supplier 不在 entity_mapping → 404（防 typo 静默 + 通用消息）。"""
    await _seedDatasource(dbSession)
    resp = await client.get(
        "/api/v1/supplier-risk/999999", headers=AUTH_HEADERS
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    # 与 supplier_360 一致：通用消息，不泄漏「不存在 vs 无权限」细节
    assert "999999" in str(body)


@pytest.mark.asyncio
async def test_get_supplier_risk_fallback_when_no_llm(
    client: AsyncClient, dbSession: AsyncSession
):
    """RISK_SCORE 缺失 + 不注入 LLM → 200 + fallback_template（异常隔离）。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100002, "SUP000002")
    # 仅 seed 3 个 fallback feature，不 seed RISK_SCORE
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000002", 80.0)
    await _seedFeatureAndValue(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", "SUP000002", 6.0)
    await _seedFeatureAndValue(dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", "SUP000002", 12.0)

    resp = await client.get(
        "/api/v1/supplier-risk/100002", headers=AUTH_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 3 feature 全违规 → High（fallback_composite）
    assert body["level"] == "high"
    assert body["levelSource"] == "fallback_composite"
    # 无 LLM → fallback_template
    assert body["riskPointsSource"] == "fallback_template"
    assert body["riskPoints"] is not None
    assert body["tokensUsed"] == 0