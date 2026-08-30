"""Phase 4.4 FeatureQueryService 单测（查询 + 目录渲染 + 计划匹配，纯逻辑）。

不连库：
- findFeature：从 QueryPlan 提取 feature_name 的匹配逻辑（conditions 优先、
  interpretation 兜底）
- _matchFeatureName：feature_name 提取正则的命中/不命中形态
- _catalogEntry：目录单条渲染格式
- _trimCatalog：超 50 条截断告警

queryValues 的 DB 路径由集成测试（真实 PG）覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.enums import EntityType, FeatureStatus
from app.domain.query_plan import QueryPlan
from app.services.feature_query_service import (
    FeatureQueryService,
    _matchFeatureName,
)


def _feature(
    name: str = "SUPPLIER_OTD_3M",
    *,
    alias: str | None = "供应商3月准时交付率",
    status: FeatureStatus = FeatureStatus.ACTIVE,
    is_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        feature_name=name,
        feature_alias=alias,
        feature_definition="供应商最近 3 个月准时交付率均值",
        entity_type=EntityType.SUPPLIER,
        window_size="3M",
        unit="%",
        status=status,
        is_enabled=is_enabled,
    )


class TestMatchFeatureName:
    def test_match_from_conditions(self) -> None:
        name = _matchFeatureName(["使用特征 SUPPLIER_OTD_3M"])
        assert name == "SUPPLIER_OTD_3M"

    def test_match_bare_name_in_conditions(self) -> None:
        name = _matchFeatureName(["SUPPLIER_DEFECT_RATE_3M 低于 0.05"])
        assert name == "SUPPLIER_DEFECT_RATE_3M"

    def test_no_match_returns_none(self) -> None:
        assert _matchFeatureName(["供应商 Q630 的交付率"]) is None

    def test_empty_conditions_returns_none(self) -> None:
        assert _matchFeatureName([]) is None

    def test_lowercase_name_not_matched(self) -> None:
        """小写词不是特征引用（FEATURE_NAME 全大写蛇形约定）。"""
        assert _matchFeatureName(["使用特征 supplier_otd_3m"]) is None


class TestFindFeature:
    def test_conditions_hit(self) -> None:
        plan = QueryPlan(
            target="供应商 OTD",
            selectedClasses=("Supplier",),
            conditions=("使用特征 SUPPLIER_OTD_3M",),
        )
        features = [_feature()]
        hit = FeatureQueryService()._matchPlanFeatures(plan, features)
        assert hit is not None and hit.feature_name == "SUPPLIER_OTD_3M"

    def test_interpretation_fallback(self) -> None:
        plan = QueryPlan(
            target="供应商 OTD",
            conditions=(),
            interpretation="问题对应预计算特征 SUPPLIER_OTD_3M",
        )
        hit = FeatureQueryService()._matchPlanFeatures(plan, [_feature()])
        assert hit is not None and hit.feature_name == "SUPPLIER_OTD_3M"

    def test_no_reference_returns_none(self) -> None:
        plan = QueryPlan(target="订单数", conditions=("2025 年",))
        assert FeatureQueryService()._matchPlanFeatures(plan, [_feature()]) is None

    def test_feature_list_empty_returns_none(self) -> None:
        plan = QueryPlan(target="x", conditions=("使用特征 SUPPLIER_OTD_3M",))
        assert FeatureQueryService()._matchPlanFeatures(plan, []) is None

    def test_unanswerable_plan_returns_none(self) -> None:
        plan = QueryPlan(target="无法回答")
        assert FeatureQueryService()._matchPlanFeatures(plan, [_feature()]) is None


class TestCatalogEntry:
    def test_renders_name_alias_entity_window_unit(self) -> None:
        entry = FeatureQueryService()._catalogEntry(_feature())
        assert entry.startswith("- SUPPLIER_OTD_3M")
        assert "供应商3月准时交付率" in entry
        assert "SUPPLIER" in entry
        assert "3M" in entry
        assert "%" in entry

    def test_missing_optional_fields_no_crash(self) -> None:
        entry = FeatureQueryService()._catalogEntry(_feature(alias=None))
        assert entry.startswith("- SUPPLIER_OTD_3M")

    def test_definition_summary_truncated(self) -> None:
        f = _feature()
        f.feature_definition = "很长的定义" * 40
        entry = FeatureQueryService()._catalogEntry(f)
        assert len(entry) <= 300


class TestTrimCatalog:
    def test_over_limit_truncates(self) -> None:
        features = [
            _feature(name=f"FEATURE_{i:03d}", alias=None) for i in range(60)
        ]
        trimmed = FeatureQueryService()._trimCatalog(features)
        assert len(trimmed) == 50

    def test_under_limit_passthrough(self) -> None:
        features = [_feature(name=f"FEATURE_{i:03d}") for i in range(10)]
        assert len(FeatureQueryService()._trimCatalog(features)) == 10


class TestCatalogValidation:
    def test_inactive_feature_excluded(self) -> None:
        svc = FeatureQueryService()
        entries = [
            svc._catalogEntry(f)
            for f in svc._filterCatalogFeatures(
                [
                    _feature(),
                    _feature(name="DRAFT_ONE", status=FeatureStatus.DRAFT),
                    _feature(name="DISABLED_ONE", is_enabled=False),
                    _feature(name="DEPRECATED", status=FeatureStatus.DEPRECATED),
                ]
            )
        ]
        assert len(entries) == 1
        assert "SUPPLIER_OTD_3M" in entries[0]

    def test_empty_catalog_renders_none(self) -> None:
        assert FeatureQueryService()._renderCatalogText([]) is None
