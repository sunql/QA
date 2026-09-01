"""Supplier Risk Agent 单测（Phase 5.4）。

覆盖：
- 主路径：RISK_SCORE 缺失/0-1 区间映射
- Fallback：3 个 feature（OTD/DEFECT/PRICE）的违规计数
- Unknown：4 个 feature 全部缺失
- LLM 不可用降级：模板生成 risk_points
- LLM 成功：记录 tokens_used + cost + model_name
- contributions 列表长度固定为 4
- actions 按等级生成
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    EntityType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue
from app.services.supplier_risk_service import SupplierRiskService

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Seed helpers（与 supplier_360_service 测试保持一致；独立以保持自包含）
# ---------------------------------------------------------------------------


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


async def _seedDataSource(dbSession: AsyncSession, *, ds_id: int = 9001) -> None:
    """插入一个最小可用的 DataSource（feature_definition.datasource_id 需要）。"""
    dbSession.add(
        DataSource(
            id=ds_id,
            name="ds-risk-test",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="x",
            username="u",
            password_encrypted="x",
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
    enabled: bool = True,
    ds_id: int = 9001,
) -> None:
    # 同一测试多次调用时，跳过已存在的 DataSource（避免重复插入冲突）
    existing = await dbSession.get(DataSource, ds_id)
    if existing is None:
        await _seedDataSource(dbSession, ds_id=ds_id)
    dbSession.add(
        FeatureDefinition(
            id=feature_id,
            feature_name=feature_name,
            feature_alias=feature_name,
            feature_definition="auto",
            entity_type=EntityType.SUPPLIER,
            calculation_logic="SELECT 1",
            window_size=window,
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit=unit,
            status=FeatureStatus.ACTIVE,
            is_enabled=enabled,
            datasource_id=ds_id,
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
# Fake LLM factory
# ---------------------------------------------------------------------------


class _FakeLlm:
    """可控的 fake LLM，固定返回含 tokens 计数的中文风险描述。"""

    def __init__(self, content: str = "LLM 生成的风险描述") -> None:
        self._content = content
        self.last_call: list[Any] | None = None

    async def complete(self, messages, **kwargs):  # noqa: ARG002
        self.last_call = list(messages)
        _Resp = type(
            "_Resp",
            (),
            {
                "content": self._content,
                "model_name": "fake-risk-model",
                "prompt_tokens": 120,
                "completion_tokens": 60,
            },
        )
        return _Resp()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_risk_score_low_at_0_85_returns_low(dbSession: AsyncSession):
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.85, unit="score", window="12M")

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "low"
    assert result.level_source == "risk_score"


async def test_risk_score_medium_at_0_70_returns_medium(dbSession: AsyncSession):
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.70, unit="score", window="12M")

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "medium"
    assert result.level_source == "risk_score"


async def test_risk_score_high_at_0_50_returns_high(dbSession: AsyncSession):
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M")

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "high"
    assert result.level_source == "risk_score"


async def test_risk_score_boundary_0_80_is_low(dbSession: AsyncSession):
    """边界值：≥ 0.80 → Low（含 0.80 本身）。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.80, unit="score", window="12M")

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "low"


async def test_fallback_high_when_all_three_violate(dbSession: AsyncSession):
    """RISK_SCORE 缺失 + OTD<90 + DEFECT>5 + PRICE>10 → High（3 违规）。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedDataSource(dbSession)
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 80.0)
    await _seedFeatureAndValue(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", "SUP000001", 6.0)
    await _seedFeatureAndValue(dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", "SUP000001", 12.0)

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "high"
    assert result.level_source == "fallback_composite"


async def test_fallback_medium_when_two_violate(dbSession: AsyncSession):
    """OTD<90 + DEFECT>5 + PRICE 正常 → Medium（2 违规）。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedDataSource(dbSession)
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 88.0)
    await _seedFeatureAndValue(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", "SUP000001", 6.0)
    await _seedFeatureAndValue(dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", "SUP000001", 5.0)

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "medium"


async def test_fallback_low_when_zero_or_one_violate(dbSession: AsyncSession):
    """仅 PRICE 超标，其他两个 OK → Low（1 违规）。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedDataSource(dbSession)
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 95.0)
    await _seedFeatureAndValue(dbSession, 2, "SUPPLIER_DEFECT_RATE_3M", "SUP000001", 2.0)
    await _seedFeatureAndValue(dbSession, 3, "SUPPLIER_PRICE_VARIANCE_3M", "SUP000001", 12.0)

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "low"


async def test_unknown_when_all_four_features_missing(dbSession: AsyncSession):
    """无任何 feature → level=Unknown。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.level.value == "unknown"
    assert result.level_source == "unknown"


async def test_contributions_have_four_items_mirroring_default_features(
    dbSession: AsyncSession,
):
    """contributions 一一映射 4 个 DEFAULT_SUPPLIER_FEATURES（不存在的 feature 也占位）。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 95.0)

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert len(result.contributions) == 4
    names = {c.feature_name for c in result.contributions}
    assert names == {
        "SUPPLIER_OTD_3M",
        "SUPPLIER_DEFECT_RATE_3M",
        "SUPPLIER_PRICE_VARIANCE_3M",
        "SUPPLIER_RISK_SCORE",
    }


async def test_llm_unavailable_falls_back_to_template(dbSession: AsyncSession):
    """llm_factory=None → risk_points 走 fallback_template，含违规 feature 名。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M")

    result = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert result.risk_points_source == "fallback_template"
    assert result.risk_points is not None
    assert "RISK_SCORE" in result.risk_points or "风险" in result.risk_points


async def test_llm_success_records_tokens_and_cost(dbSession: AsyncSession):
    """LLM 可用 → risk_points_source=llm + tokens_used > 0 + cost > 0 + llm_model_name 非空。"""
    fake = _FakeLlm()
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M")

    result = await SupplierRiskService().assess(
        dbSession, 100001, llm_factory=lambda cfg: fake  # noqa: ARG005
    )

    assert result.risk_points_source == "llm"
    assert result.tokens_used == 180  # 120 + 60
    assert result.cost > 0
    assert result.llm_model_name == "fake-risk-model"
    assert result.risk_points == "LLM 生成的风险描述"


async def test_actions_per_level(dbSession: AsyncSession):
    """各等级都有 ≥1 条 recommended_actions。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M")

    result_high = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)

    assert len(result_high.recommended_actions) >= 1


async def test_assess_404_when_supplier_not_in_mapping(dbSession: AsyncSession):
    """无 entity_mapping → 抛 NotFoundError。"""
    from app.domain.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await SupplierRiskService().assess(dbSession, 999999, llm_factory=None)


async def test_contribution_passed_flag(dbSession: AsyncSession):
    """OTD=80（<90 阈值）→ passed=False；OTD=95（≥90）→ passed=True。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedDataSource(dbSession)
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 80.0)

    result_low = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)
    otd_low = next(c for c in result_low.contributions if c.feature_name == "SUPPLIER_OTD_3M")
    assert otd_low.passed is False

    # 再 seed 一个高 OTD 值（覆盖）
    from sqlalchemy import delete
    from app.domain.models import FeatureDefinition, FeatureValue

    await dbSession.execute(
        delete(FeatureValue).where(FeatureValue.entity_key == "SUP000001")
    )
    await dbSession.execute(
        delete(FeatureDefinition).where(FeatureDefinition.id == 1)
    )
    await dbSession.commit()
    await _seedFeatureAndValue(dbSession, 1, "SUPPLIER_OTD_3M", "SUP000001", 95.0)

    result_high = await SupplierRiskService().assess(dbSession, 100001, llm_factory=None)
    otd_high = next(c for c in result_high.contributions if c.feature_name == "SUPPLIER_OTD_3M")
    assert otd_high.passed is True


# ---------------------------------------------------------------------------
# Phase 6.x：service 入口 str|int 双路解析（与 supplier_360_service 对齐）
# ---------------------------------------------------------------------------


async def test_assess_accepts_str_enterprise_code(dbSession: AsyncSession):
    """Phase 6.x：用 VARCHAR enterprise_code 调用 SupplierRiskService.assess 应命中。

    这是用户面对的"供应商编码"形态（THBI '10105'）。Agent 工具路径
    （agent_tools._supplierRiskHandler）现已透传 str 而非 int()，service 必须支持。
    """
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.85, unit="score", window="12M"
    )

    # str 路径 → _resolveSupplier Pass 1（enterprise_code）命中
    result = await SupplierRiskService().assess(
        dbSession, "SUP000001", llm_factory=None
    )

    assert result.profile.enterprise_code == "SUP000001"
    assert result.profile.enterprise_key == 100001
    assert result.level.value == "low"


async def test_assess_accepts_int_enterprise_key_backward_compat(
    dbSession: AsyncSession,
):
    """Phase 6.x：向后兼容 BIGINT enterprise_key（chat_service / API 旧调用点）。"""
    await _seedSupplier(dbSession, 100001, "SUP000001")
    await _seedFeatureAndValue(
        dbSession, 1, "SUPPLIER_RISK_SCORE", "SUP000001", 0.50, unit="score", window="12M"
    )

    result = await SupplierRiskService().assess(
        dbSession, 100001, llm_factory=None
    )

    assert result.profile.enterprise_code == "SUP000001"
    assert result.level.value == "high"


async def test_assess_str_not_found_raises(dbSession: AsyncSession):
    """Phase 6.x：str 入参找不到 → NotFoundError。"""
    from app.domain.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await SupplierRiskService().assess(
            dbSession, "DOES_NOT_EXIST", llm_factory=None
        )