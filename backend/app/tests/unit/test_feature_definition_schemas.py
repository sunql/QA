"""Phase 4.3 AI Feature Schema/Enum 单测（不依赖 DB/外部服务）。

覆盖：
1. FeatureStatus / FeatureRefreshFrequency 枚举三态
2. FeatureDefinitionCreate/Update/Read DTO：字段定义 + 长度上限 + 默认值
3. model_dump 序列化输出 camelCase JSON（与 FastAPI 契约一致）
4. 非法 status / 超长 feature_name 拒绝
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.enums import (
    FeatureRefreshFrequency,
    FeatureStatus,
)
from app.domain.schemas import (
    FeatureDefinitionCreate,
    FeatureDefinitionRead,
    FeatureDefinitionUpdate,
    FeatureValueRead,
)


class TestFeatureEnums:
    def test_feature_status_three_values(self) -> None:
        assert {m.value for m in FeatureStatus} == {"DRAFT", "ACTIVE", "DEPRECATED"}

    def test_feature_refresh_frequency_three_values(self) -> None:
        assert {m.value for m in FeatureRefreshFrequency} == {
            "DAILY",
            "WEEKLY",
            "MONTHLY",
        }

    def test_status_values_db_compatible(self) -> None:
        for m in FeatureStatus:
            assert len(m.value) <= 20


class TestFeatureDefinitionSchemas:
    def test_create_required_fields(self) -> None:
        dto = FeatureDefinitionCreate(
            featureName="SUPPLIER_OTD_3M",
            entityType="SUPPLIER",
            calculationLogic="SELECT supplier_code AS entity_key, AVG(otd_rate) AS value FROM DWS WHERE 1=1",
            datasourceId=4,
        )
        assert dto.feature_name == "SUPPLIER_OTD_3M"
        assert dto.entity_type == "SUPPLIER"
        # 默认值
        assert dto.status == FeatureStatus.DRAFT
        assert dto.refresh_frequency == FeatureRefreshFrequency.DAILY
        assert dto.is_enabled is True
        assert dto.version is None  # service 兜底 v1.0
        assert "owner" not in FeatureDefinitionCreate.model_fields  # owner 由服务端派生，不在 DTO 中
        # created_by 亦由 actor.userId 派生，不接受 client 声明（M1）
        assert "created_by" not in FeatureDefinitionCreate.model_fields

    def test_create_rejects_nonpositive_datasource_id(self) -> None:
        with pytest.raises(ValidationError):
            FeatureDefinitionCreate(
                featureName="F1",
                entityType="SUPPLIER",
                calculationLogic="SELECT 1",
                datasourceId=0,
            )

    def test_create_rejects_oversized_feature_name(self) -> None:
        with pytest.raises(ValidationError):
            FeatureDefinitionCreate(
                featureName="x" * 101,
                entityType="SUPPLIER",
                calculationLogic="SELECT 1",
                datasourceId=4,
            )

    def test_create_rejects_empty_calculation_logic(self) -> None:
        with pytest.raises(ValidationError):
            FeatureDefinitionCreate(
                featureName="F1",
                entityType="SUPPLIER",
                calculationLogic="",
                datasourceId=4,
            )

    def test_create_rejects_invalid_entity_type(self) -> None:
        with pytest.raises(ValidationError):
            FeatureDefinitionCreate(
                featureName="F1",
                entityType="BOGUS",
                calculationLogic="SELECT 1",
                datasourceId=4,
            )

    def test_create_rejects_invalid_status(self) -> None:
        with pytest.raises(ValidationError):
            FeatureDefinitionCreate(
                featureName="F1",
                entityType="SUPPLIER",
                calculationLogic="SELECT 1",
                datasourceId=4,
                status="BOGUS",
            )

    def test_update_partial_fields(self) -> None:
        dto = FeatureDefinitionUpdate(status="ACTIVE", unit="%")
        assert dto.status == FeatureStatus.ACTIVE
        assert dto.unit == "%"
        assert dto.calculation_logic is None  # 未设置

    def test_update_clear_nullable_field_with_none(self) -> None:
        """可空字段（window_size/unit）显式传 null 清空。"""
        dto = FeatureDefinitionUpdate(windowSize=None, unit=None)
        dumped = dto.model_dump(exclude_unset=True)
        assert dumped["window_size"] is None
        assert dumped["unit"] is None

    def test_read_dto_carries_all_fields(self) -> None:
        from datetime import datetime

        read = FeatureDefinitionRead(
            id=1,
            featureName="SUPPLIER_OTD_3M",
            entityType="SUPPLIER",
            calculationLogic="SELECT 1",
            refreshFrequency="DAILY",
            version="v1.0",
            status="DRAFT",
            isEnabled=True,
            datasourceId=4,
            createdTime=datetime(2026, 1, 1),
            updatedTime=datetime(2026, 1, 2),
        )
        assert read.id == 1
        assert read.status == FeatureStatus.DRAFT
        assert read.owner is None

    def test_model_dump_json_by_alias_camelcase(self) -> None:
        dto = FeatureDefinitionCreate(
            featureName="F1",
            entityType="SUPPLIER",
            calculationLogic="SELECT 1",
            datasourceId=4,
        )
        dumped = dto.model_dump(mode="json", by_alias=True)
        assert "featureName" in dumped
        assert "entityType" in dumped
        assert "calculationLogic" in dumped
        assert "datasourceId" in dumped
        assert dumped["status"] == "DRAFT"


class TestFeatureValueSchema:
    def test_value_read_roundtrip(self) -> None:
        from datetime import date, datetime
        from decimal import Decimal

        read = FeatureValueRead(
            id=1,
            featureId=10,
            entityKey="Q630",
            value=Decimal("0.9500"),
            valueText=None,
            validAt=date(2026, 7, 31),
            computedAt=datetime(2026, 8, 1, 12, 0, 0),
        )
        assert read.value == Decimal("0.9500")
        assert read.value_text is None
        assert read.entity_key == "Q630"
