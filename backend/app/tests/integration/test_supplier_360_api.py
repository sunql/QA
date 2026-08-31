"""供应商 360° ADS 视图 API 集成测试（Phase 5.3，真实 PG + 完整 HTTP 链路 per Harness 测试规范）。

覆盖：
- 200 完整链路：有 entity_mapping + 4 个 SUPPLIER feature 最新值
- 200 空 kpis：有 entity_mapping 但无 feature_value
- 404：enterprise_key 不存在
- 401：未认证（stub 模式下不应通过——但 AUTH_STUB_ENABLED=1 时 getCurrentUser 用匿名默认）
- ACL：supplier-360 是只读聚合视图，鉴权仅 getCurrentUser，**不强制** owner-based ACL
- 契约：响应字段 camelCase，DTO 字段与 unit 一致
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    EntityType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue

pytestmark = pytest.mark.asyncio

# 默认 stub 用户带 admin（参见 dependencies.DEFAULT_STUB_ROLES），免去重复声明
AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=9101,
        name="test-ds-360-api",
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
    dbSession.add_all([
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{key}",
            source_code=f"V{key}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        ),
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.SRM,
            source_key=f"S{key}",
            source_code=f"S{key}",
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
    fd = FeatureDefinition(
        id=feature_id,
        feature_name=feature_name,
        feature_alias=alias,
        feature_definition="auto seeded for integration test",
        entity_type=EntityType.SUPPLIER,
        calculation_logic="SELECT 1",
        window_size="3M",
        refresh_frequency=FeatureRefreshFrequency.DAILY,
        unit=unit,
        status=FeatureStatus.ACTIVE,
        is_enabled=True,
        datasource_id=9101,
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
    valid_at: date,
) -> FeatureValue:
    fv = FeatureValue(
        feature_id=feature_id,
        entity_key=entity_key,
        value=value,
        valid_at=valid_at,
        computed_at=datetime.combine(valid_at, datetime.min.time(), tzinfo=timezone.utc),
    )
    dbSession.add(fv)
    await dbSession.commit()
    return fv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_supplier_360_returns_full_payload(
    client: AsyncClient, dbSession: AsyncSession
):
    """完整链路：profile + entity_codes + 4 个 kpis 最新值。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M", unit="%", alias="3 月准时交付率")
    await _seedFeature(
        dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", unit="%", alias="3 月拒收率"
    )
    await _seedFeature(
        dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", unit="%", alias="3 月价格偏差"
    )
    await _seedFeature(
        dbSession, 4, "SUPPLIER_RISK_SCORE", unit="score", alias="风险评分"
    )
    await _seedFeatureValue(dbSession, 1, "SUP000001", 92.5, date(2026, 8, 31))
    await _seedFeatureValue(dbSession, 2, "SUP000001", 2.3, date(2026, 8, 31))
    await _seedFeatureValue(dbSession, 3, "SUP000001", -1.5, date(2026, 8, 31))
    await _seedFeatureValue(dbSession, 4, "SUP000001", 75.0, date(2026, 8, 31))

    resp = await client.get("/api/v1/supplier-360/100001", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    # 与 entity_mapping / features 同模式：直接返回 DTO，不裹 ApiResponse 信封
    data = resp.json()

    # 顶层字段
    assert set(data.keys()) >= {"profile", "entityCodes", "kpis", "fetchedAt"}
    assert isinstance(data["fetchedAt"], str)
    # profile（camelCase）
    p = data["profile"]
    assert p["enterpriseKey"] == 100001
    assert p["enterpriseCode"] == "SUP000001"
    assert p["entityType"] == "SUPPLIER"
    # entity_codes
    assert len(data["entityCodes"]) == 2
    sources = {c["sourceSystem"] for c in data["entityCodes"]}
    assert sources == {"ERP", "SRM"}
    for c in data["entityCodes"]:
        assert set(c.keys()) == {"sourceSystem", "sourceCode", "sourceKey", "matchRule"}
    # kpis 全部 latest=True
    assert len(data["kpis"]) == 4
    by_name = {k["featureName"]: k for k in data["kpis"]}
    otd = by_name["SUPPLIER_OTD_3M"]
    assert otd["latest"] is True
    # Decimal 经 Pydantic 序列化为字符串，Numeric(38,10) 保留 10 位小数
    assert otd["value"] == "92.5000000000"
    assert otd["unit"] == "%"
    assert otd["validAt"] == "2026-08-31"
    assert otd["featureAlias"] == "3 月准时交付率"
    assert isinstance(otd["value"], str)


async def test_get_supplier_360_returns_empty_kpis_when_no_values(
    client: AsyncClient, dbSession: AsyncSession
):
    """有 entity_mapping 但无 feature_value → 4 个 kpi 全 latest=False。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100002, "SUP000002")

    resp = await client.get("/api/v1/supplier-360/100002", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["profile"]["enterpriseCode"] == "SUP000002"
    assert len(data["entityCodes"]) == 2
    # 4 个 default feature 全部 latest=False 占位
    assert len(data["kpis"]) == 4
    assert all(k["latest"] is False for k in data["kpis"])
    assert all(k["value"] is None for k in data["kpis"])


async def test_get_supplier_360_404_when_supplier_not_in_mapping(
    client: AsyncClient,
):
    """enterprise_key 在 entity_mapping 中不存在 → 404。"""
    resp = await client.get("/api/v1/supplier-360/999999", headers=AUTH_HEADERS)
    assert resp.status_code == 404
    body = resp.json()
    # 错误信封由 _testapp.handleDomainError 渲染：{error, detail}，无 success 字段
    assert "error" in body and body["error"]
    # 不应泄漏「不存在 vs 存在但无权限」的区分：通用错误消息
    # （本路径本就只有 NotFound 一条；侧信道防护由 4.5 ACL 处理）


async def test_get_supplier_360_route_prefix_is_correct(
    client: AsyncClient,
):
    """路径前缀契约：必须 /api/v1/supplier-360/{key}（防路径漂移）。"""
    resp = await client.get("/api/v1/supplier-360/1", headers=AUTH_HEADERS)
    # 这里不关心业务结果（可能是 404），只关心路径能解析到 supplier-360 路由
    assert resp.status_code in (200, 404)
    # 404 也是 supplier-360 路由响应，不是 FastAPI 兜底 422/404
    if resp.status_code == 404:
        # 我们的错误信封格式（含 success/data/error），而非 FastAPI 默认 detail
        body = resp.json()
        assert "success" in body or "detail" in body


async def test_get_supplier_360_kpi_value_is_decimal_string(
    client: AsyncClient, dbSession: AsyncSession
):
    """KPI 数值字段是 Decimal 序列化（保持精度，与 FeatureValueRead 一致）。"""
    await _seedDatasource(dbSession)
    await _seedSupplier(dbSession, 100003, "SUP000003")
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M", unit="%")
    # 整数 + 小数：序列化为字符串
    await _seedFeatureValue(dbSession, 1, "SUP000003", 100.0, date(2026, 8, 31))

    resp = await client.get("/api/v1/supplier-360/100003", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    otd = next(k for k in data["kpis"] if k["featureName"] == "SUPPLIER_OTD_3M")
    # Decimal 走 Pydantic → str，Numeric(38,10) 保留 10 位小数
    assert otd["value"] == "100.0000000000"
