"""Task 0.2：验证 Supplier 360 自动加载新 FeatureDefinition。

验证逻辑：
1. 在 DB 中 seed SUPPLIER_PO_COMPLETION_RATE FeatureDefinition（Task 0.1 已落）
2. 调用 Supplier360Service.get360(supplier_code)
3. 断言返回的 KPIs 列表中包含 SUPPLIER_PO_COMPLETION_RATE

核心假设（Task 0.2 brief §关键决策）：
- _kpiSlotFeatureNames() 通过 feature_rule_registry.getEnabledRules() 自动发现
  所有 enabled FeatureDefinition，无需改 Supplier360Service 代码
- 如果发现假设不成立（硬编码 / 缺少 FeatureRule），停下来写报告，不自动修

真实 PG + 完整 API 链路（per Harness 测试规范）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import FeatureRefreshFrequency, FeatureStatus
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue
from app.services.feature_rule_registry import feature_rule_registry

pytestmark = pytest.mark.asyncio

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=9201,
        name="test-ds-360-po-completion",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="test_db",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds


async def _seedSupplier(dbSession: AsyncSession, key: int, code: str) -> None:
    from app.domain.enums import MatchRule, SourceSystem
    dbSession.add_all([
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        ),
    ])
    await dbSession.commit()


async def _seedFeature(
    dbSession: AsyncSession,
    feature_id: int,
    feature_name: str,
    *,
    unit: str | None = None,
    alias: str | None = None,
) -> FeatureDefinition:
    """Seed a single SUPPLIER FeatureDefinition (mirrors _seedFeature in test_supplier_360_api)."""
    fd = FeatureDefinition(
        id=feature_id,
        feature_name=feature_name,
        feature_alias=alias,
        feature_definition="auto seeded for Task 0.2 integration test",
        entity_type="SUPPLIER",
        calculation_logic="SELECT 1",
        window_size="3M",
        refresh_frequency=FeatureRefreshFrequency.DAILY,
        unit=unit,
        status=FeatureStatus.ACTIVE,
        is_enabled=True,
        datasource_id=9201,
        version="v1.0",
    )
    dbSession.add(fd)
    await dbSession.commit()
    return fd


async def _seedFeatureValue(
    dbSession: AsyncSession,
    feature_id: int,
    entity_key: str,
    value: float,
) -> FeatureValue:
    """Seed a single FeatureValue (simplified date=today)."""
    from datetime import date, datetime, timezone
    today = date.today()
    fv = FeatureValue(
        feature_id=feature_id,
        entity_key=entity_key,
        value=value,
        valid_at=today,
        computed_at=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
    )
    dbSession.add(fv)
    await dbSession.commit()
    return fv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_supplier_360_loads_po_completion_rate(
    client: AsyncClient, dbSession: AsyncSession
):
    """验证 get360 返回的 KPIs 中包含 SUPPLIER_PO_COMPLETION_RATE。

    Arrange:
    - entity_mapping（SUP000101, enterprise_key=100101）
    - 5 个 SUPPLIER FeatureDefinition（OTD / DEFECT / PRICE / RISK / PO_COMPLETION）
    - 5 个 FeatureValue（每个 feature 各一条，value=85.0）

    Act:
    - GET /api/v1/supplier-360/100101

    Assert:
    - HTTP 200
    - kpis 长度为 5
    - 其中一个 kpi 的 featureName == "SUPPLIER_PO_COMPLETION_RATE"
    - 该 kpi latest == True，value == "85.0"
    """
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100101, "SUP000101")

    # Seed 5 个 FeatureDefinition（4 个原有 + PO_COMPLETION_RATE）
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M", unit="%", alias="3月准时交付率")
    await _seedFeature(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", unit="%", alias="3月拒收率")
    await _seedFeature(dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", unit="%", alias="3月价格偏差")
    await _seedFeature(dbSession, 4, "SUPPLIER_RISK_SCORE", unit="score", alias="风险评分")
    await _seedFeature(dbSession, 5, "SUPPLIER_PO_COMPLETION_RATE", unit="%", alias="采购订单完成率")

    # Seed 5 个 FeatureValue
    await _seedFeatureValue(dbSession, 1, "SUP000101", 92.5)
    await _seedFeatureValue(dbSession, 2, "SUP000101", 2.3)
    await _seedFeatureValue(dbSession, 3, "SUP000101", -1.5)
    await _seedFeatureValue(dbSession, 4, "SUP000101", 75.0)
    await _seedFeatureValue(dbSession, 5, "SUP000101", 85.0)

    # Invalidate and re-warm feature_rule_registry so new FeatureDefinition is visible
    # (but registry is keyed by FeatureRule, not FeatureDefinition — see concerns)
    feature_rule_registry.invalidate()
    await feature_rule_registry.warmUp(dbSession)

    resp = await client.get("/api/v1/supplier-360/100101", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    kpis = data["kpis"]
    feature_names = {k["featureName"] for k in kpis}

    # 核心断言：PO_COMPLETION_RATE 必须出现在 KPIs 中
    assert "SUPPLIER_PO_COMPLETION_RATE" in feature_names, (
        f"SUPPLIER_PO_COMPLETION_RATE not found in KPIs. Got: {sorted(feature_names)}"
    )

    # 断言有 5 个 KPI（4 原有 + PO_COMPLETION_RATE）
    assert len(kpis) == 5, f"Expected 5 KPIs, got {len(kpis)}: {[k['featureName'] for k in kpis]}"

    # 验证 PO_COMPLETION_RATE 的值
    po_kpi = next(k for k in kpis if k["featureName"] == "SUPPLIER_PO_COMPLETION_RATE")
    assert po_kpi["latest"] is True, f"PO_COMPLETION_RATE latest should be True, got {po_kpi['latest']}"
    assert po_kpi["value"] == "85.0000000000", f"PO_COMPLETION_RATE value should be 85.0000000000, got {po_kpi['value']}"
    assert po_kpi["unit"] == "%"
    assert po_kpi["featureAlias"] == "采购订单完成率"


async def test_supplier_360_po_completion_rate_without_value_is_latest_false(
    client: AsyncClient, dbSession: AsyncSession
):
    """验证有 FeatureDefinition 但无 FeatureValue 时，PO_COMPLETION_RATE 返回 latest=False。

    Arrange:
    - entity_mapping
    - SUPPLIER_PO_COMPLETION_RATE FeatureDefinition（enabled + ACTIVE）
    - 无对应的 FeatureValue

    Act:
    - GET /api/v1/supplier-360/100102

    Assert:
    - kpis 中有 SUPPLIER_PO_COMPLETION_RATE 且 latest=False
    """
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100102, "SUP000102")
    await _seedFeature(dbSession, 5, "SUPPLIER_PO_COMPLETION_RATE", unit="%", alias="采购订单完成率")

    feature_rule_registry.invalidate()
    await feature_rule_registry.warmUp(dbSession)

    resp = await client.get("/api/v1/supplier-360/100102", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    po_kpi = next(
        (k for k in data["kpis"] if k["featureName"] == "SUPPLIER_PO_COMPLETION_RATE"),
        None
    )
    assert po_kpi is not None, "SUPPLIER_PO_COMPLETION_RATE not in KPIs at all"
    assert po_kpi["latest"] is False, f"Expected latest=False, got {po_kpi['latest']}"
    assert po_kpi["value"] is None
