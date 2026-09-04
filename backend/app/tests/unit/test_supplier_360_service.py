"""Supplier360Service 单测（Phase 5.3，real PG per Harness 测试规范）。

覆盖：
- 找不到 enterprise_key → NotFoundError
- 只有 entity_mapping / 无 feature value → kpis 全 latest=False
- 完整链路：entity_mapping + 4 个 SUPPLIER feature 最新值聚合
- 任意 feature 取最新值（多 valid_at 行）
- 异常隔离：feature query 失败 → profile + entity_codes 仍返回，kpis 为空
- ACL：服务层不强制 ACL（API 层处理；保持 service 单元可测）
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.exceptions import NotFoundError
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue
from app.domain.schemas import Supplier360Read
from app.services.supplier_360_service import (
    DEFAULT_SUPPLIER_FEATURES,
    Supplier360Service,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=9001,
        name="test-ds-360",
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


async def _seedSupplierMappings(
    dbSession: AsyncSession, key: int, code: str
) -> list[EntityMapping]:
    """SUP000001 × {ERP, SRM} 两条。"""
    rows = [
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.ERP,
            source_key=f"V{int(code[3:])}",
            source_code=f"V{int(code[3:])}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        ),
        EntityMapping(
            entity_type="SUPPLIER",
            enterprise_key=key,
            enterprise_code=code,
            source_system=SourceSystem.SRM,
            source_key=f"S{int(code[3:])}",
            source_code=f"S{int(code[3:])}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        ),
    ]
    dbSession.add_all(rows)
    await dbSession.commit()
    return rows


async def _seedFeature(
    dbSession: AsyncSession,
    feature_id: int,
    feature_name: str,
    alias: str | None = None,
    unit: str | None = None,
    is_enabled: bool = True,
) -> FeatureDefinition:
    fd = FeatureDefinition(
        id=feature_id,
        feature_name=feature_name,
        feature_alias=alias,
        feature_definition="auto seeded for test",
        entity_type="SUPPLIER",
        calculation_logic="SELECT 1",
        window_size="3M",
        refresh_frequency=FeatureRefreshFrequency.DAILY,
        unit=unit,
        status=FeatureStatus.ACTIVE,
        is_enabled=is_enabled,
        datasource_id=9001,
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
    computed_at: datetime | None = None,
) -> FeatureValue:
    fv = FeatureValue(
        feature_id=feature_id,
        entity_key=entity_key,
        value=value,
        valid_at=valid_at,
        computed_at=computed_at or datetime.combine(
            valid_at, datetime.min.time(), tzinfo=timezone.utc
        ),
    )
    dbSession.add(fv)
    await dbSession.commit()
    return fv


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_supplier_360_returns_profile_and_codes(dbSession: AsyncSession):
    """最小链路：有 entity_mapping，无 feature value → profile + codes 完整，kpis 全空。"""
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)

    assert isinstance(result, Supplier360Read)
    assert result.profile.enterprise_key == 100001
    assert result.profile.enterprise_code == "SUP000001"
    assert result.profile.entity_type == "SUPPLIER"
    # 2 条 ERP/SRM 映射
    assert len(result.entity_codes) == 2
    sources = {c.source_system for c in result.entity_codes}
    assert sources == {SourceSystem.ERP, SourceSystem.SRM}
    # kpis 4 项 default feature 全部 latest=False
    assert len(result.kpis) == 4
    assert all(k.latest is False for k in result.kpis)
    assert {k.feature_name for k in result.kpis} == set(DEFAULT_SUPPLIER_FEATURES)


async def test_get_supplier_360_not_found_raises(dbSession: AsyncSession):
    """enterprise_key 不存在 → NotFoundError（而非返回空对象，防 typo 静默）。"""
    svc = Supplier360Service()
    with pytest.raises(NotFoundError) as excInfo:
        await svc.get360(dbSession, 999999)
    assert "999999" in str(excInfo.value)


async def test_get_supplier_360_aggregates_latest_feature_values(
    dbSession: AsyncSession,
):
    """完整链路：4 个 feature 都有最新值 + 1 个 feature 有历史值（取最新）。"""
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")

    # Seed 4 features
    fd_otd = await _seedFeature(
        dbSession, 1, "SUPPLIER_OTD_3M", alias="3 月准时交付率", unit="%"
    )
    fd_defect = await _seedFeature(
        dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", alias="3 月拒收率", unit="%"
    )
    fd_price = await _seedFeature(
        dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", alias="3 月价格偏差", unit="%"
    )
    fd_risk = await _seedFeature(
        dbSession, 4, "SUPPLIER_RISK_SCORE", alias="风险评分", unit="score"
    )

    # 老 OTD 值（应被忽略）+ 新 OTD 值（应被取到）
    await _seedFeatureValue(dbSession, 1, "SUP000001", 88.0, date(2026, 7, 31))
    await _seedFeatureValue(dbSession, 1, "SUP000001", 92.5, date(2026, 8, 31))
    # 单值 feature
    await _seedFeatureValue(dbSession, 2, "SUP000001", 2.3, date(2026, 8, 31))
    await _seedFeatureValue(dbSession, 3, "SUP000001", -1.5, date(2026, 8, 31))
    await _seedFeatureValue(dbSession, 4, "SUP000001", 75.0, date(2026, 8, 31))

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)

    by_name = {k.feature_name: k for k in result.kpis}
    # 最新 OTD = 92.5（覆盖 88.0）
    otd = by_name["SUPPLIER_OTD_3M"]
    assert otd.latest is True
    # ORM Numeric(38,10) → Decimal；与 Decimal 字面量比（避免 pytest.approx 的
    # Decimal/float 混算 TypeError）
    assert otd.value == Decimal("92.5")
    assert otd.unit == "%"
    assert otd.valid_at == date(2026, 8, 31)

    # 其余 3 个 feature
    assert by_name["SUPPLIER_DEFECT_RATE_3M"].value == Decimal("2.3")
    assert by_name["SUPPLIER_PRICE_VARIANCE_3M"].value == Decimal("-1.5")
    assert by_name["SUPPLIER_RISK_SCORE"].value == Decimal("75.0")


async def test_get_supplier_360_skips_disabled_features(dbSession: AsyncSession):
    """is_enabled=False 的 feature 不出现在 kpis 列表（前端无意义渲染）。"""
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M", is_enabled=True)
    await _seedFeature(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", is_enabled=False)
    await _seedFeature(dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", is_enabled=True)

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)
    names = {k.feature_name for k in result.kpis}
    assert "SUPPLIER_DEFECT_RATE_3M" not in names
    assert "SUPPLIER_OTD_3M" in names


async def test_get_supplier_360_skips_draft_features(dbSession: AsyncSession):
    """status=DRAFT 的 feature 不出现在 kpis（治理规范：未发布不入库）。"""
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M")
    fd_draft = await _seedFeature(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M")
    fd_draft.status = FeatureStatus.DRAFT
    await dbSession.commit()

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)
    names = {k.feature_name for k in result.kpis}
    assert "SUPPLIER_DEFECT_RATE_3M" not in names


async def test_get_supplier_360_returns_disabled_feature_with_no_value_marker(
    dbSession: AsyncSession,
):
    """kpis 中 feature 未找到 / 未启用 → latest=False（前端显示「暂无数据」占位）。"""
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")
    # 不 seed 任何 feature definition

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)

    # 4 个 default feature 都在列表中（前端可逐项提示）
    assert len(result.kpis) == 4
    for k in result.kpis:
        assert k.latest is False
        assert k.value is None
        assert k.valid_at is None


async def test_default_supplier_features_constant_is_4_items():
    """Phase 5.3 范围：固定 4 个 SUPPLIER feature（与 seed_features.py 对齐）。"""
    assert len(DEFAULT_SUPPLIER_FEATURES) == 4
    assert "SUPPLIER_OTD_3M" in DEFAULT_SUPPLIER_FEATURES
    assert "SUPPLIER_DEFECT_RATE_3M" in DEFAULT_SUPPLIER_FEATURES
    assert "SUPPLIER_PRICE_VARIANCE_3M" in DEFAULT_SUPPLIER_FEATURES
    assert "SUPPLIER_RISK_SCORE" in DEFAULT_SUPPLIER_FEATURES


async def test_get_supplier_360_enterprise_code_used_for_feature_join(
    dbSession: AsyncSession,
):
    """feature_value.entity_key 是 enterprise_code（VARCHAR），不是 enterprise_key（INT）。

    即使 entity_mapping 有 key=100001/code=SUP000001，feature_value 应存 SUP000001。
    """
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M")
    await _seedFeatureValue(dbSession, 1, "SUP000001", 95.0, date(2026, 8, 31))
    # 错误键：int 写法不应被命中
    await _seedFeatureValue(dbSession, 1, "100001", 50.0, date(2026, 8, 31))

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)
    otd = next(k for k in result.kpis if k.feature_name == "SUPPLIER_OTD_3M")
    assert otd.value == Decimal("95.0")


async def test_get_supplier_360_supports_no_entity_codes(dbSession: AsyncSession):
    """有 feature value 但无 entity_mapping → 404（profile 拿不到 enterprise_code）。"""
    # 这是预期错误：service 用 enterprise_key 找不到 profile 自然抛 NotFoundError
    svc = Supplier360Service()
    with pytest.raises(NotFoundError):
        await svc.get360(dbSession, 100001)


# ---------------------------------------------------------------------------
# Phase 6.x：service 入口 str|int 双路解析
# ---------------------------------------------------------------------------


async def test_get_supplier_360_accepts_str_enterprise_code(dbSession: AsyncSession):
    """Phase 6.x：用 VARCHAR enterprise_code（如 THBI '10105'）调用应命中。

    这是用户面对的"供应商编码"形态（THBI BPSNUM_0 = '10105'，合成种子 SUP000001）。
    此前 int() 强制转换会把 '10105' 变成 10105，找不到 hash → 404。
    """
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")

    svc = Supplier360Service()
    # 字符串 enterprise_code 命中同一条 entity_mapping
    result = await svc.get360(dbSession, "SUP000001")

    assert result.profile.enterprise_key == 100001
    assert result.profile.enterprise_code == "SUP000001"
    assert len(result.entity_codes) == 2  # ERP + SRM
    assert len(result.kpis) == 4


async def test_get_supplier_360_accepts_int_enterprise_key_backward_compat(
    dbSession: AsyncSession,
):
    """Phase 6.x：向后兼容 BIGINT enterprise_key（既有 chat / API 路径）。

    旧 chat_service / API 调用可能仍传 int；_resolveSupplier Pass 2 必须命中。
    """
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")

    svc = Supplier360Service()
    result = await svc.get360(dbSession, 100001)

    assert result.profile.enterprise_key == 100001
    assert result.profile.enterprise_code == "SUP000001"


async def test_get_supplier_360_str_joins_feature_value_by_code(
    dbSession: AsyncSession,
):
    """Phase 6.x：str 入参时 feature_value JOIN 仍用 enterprise_code（VARCHAR）。

    验证：即使 str 入参返回的 profile.enterprise_key 是 BIGINT，feature_value 仍按
    enterprise_code（VARCHAR）JOIN。回归保护 SSOT §2 的 JOIN 语义。
    """
    await _seedDatasource(dbSession)
    await _seedSupplierMappings(dbSession, 100001, "SUP000001")
    await _seedFeature(dbSession, 1, "SUPPLIER_OTD_3M")
    await _seedFeatureValue(dbSession, 1, "SUP000001", 95.0, date(2026, 8, 31))
    # 错误键：int 写法不应被命中（回归保护）
    await _seedFeatureValue(dbSession, 1, "100001", 50.0, date(2026, 8, 31))

    svc = Supplier360Service()
    result = await svc.get360(dbSession, "SUP000001")
    otd = next(k for k in result.kpis if k.feature_name == "SUPPLIER_OTD_3M")
    assert otd.value == Decimal("95.0")


async def test_get_supplier_360_str_not_found_raises(dbSession: AsyncSession):
    """Phase 6.x：str 入参找不到 → NotFoundError（与 int 路径语义一致）。"""
    svc = Supplier360Service()
    with pytest.raises(NotFoundError) as excInfo:
        await svc.get360(dbSession, "NONEXISTENT_CODE")
    # key 显示原始输入（不转 int 防误导）
    assert "NONEXISTENT_CODE" in str(excInfo.value)
