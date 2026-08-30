"""Phase 4.1 KPI Catalog Schema/Enum 单测（不依赖 DB/外部服务）。

覆盖：
1. KpiStatus 枚举：DRAFT/PUBLISHED/DEPRECATED + 值校验
2. KpiCatalogCreate/Update/Read DTO：字段定义 + 长度上限
3. model_dump 序列化输出 camelCase JSON（与 FastAPI 契约一致）
4. revision_count 默认 0；update 不传字段不动
"""

from __future__ import annotations

from app.domain.enums import KpiStatus
from app.domain.schemas import (
    KpiCatalogCreate,
    KpiCatalogRead,
    KpiCatalogUpdate,
)


class TestKpiStatusEnum:
    def test_has_three_values(self) -> None:
        assert {m.value for m in KpiStatus} == {"DRAFT", "PUBLISHED", "DEPRECATED"}

    def test_value_strings_usable_in_db(self) -> None:
        # VARCHAR(20) 兼容
        for m in KpiStatus:
            assert len(m.value) <= 20


class TestKpiCatalogSchemas:
    def test_create_required_fields(self) -> None:
        dto = KpiCatalogCreate(
            kpiCode="KPI_SUPPLIER_OTD",
            kpiName="供应商准时交付率",
        )
        assert dto.kpi_code == "KPI_SUPPLIER_OTD"
        assert dto.kpi_name == "供应商准时交付率"
        # 默认值
        assert dto.status == KpiStatus.DRAFT
        # version / revision_count 由 service 兜底（DTO 默认 None）
        assert dto.version is None
        # 可选字段
        assert dto.business_definition is None
        assert dto.metric_id is None
        assert dto.owner is None
        assert dto.unit is None

    def test_create_rejects_oversized_code(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            KpiCatalogCreate(kpiCode="x" * 51, kpiName="n")

    def test_create_rejects_empty_name(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            KpiCatalogCreate(kpiCode="K1", kpiName="")

    def test_update_partial_fields(self) -> None:
        dto = KpiCatalogUpdate(status="PUBLISHED", owner="采购部")
        assert dto.status == KpiStatus.PUBLISHED
        assert dto.owner == "采购部"
        assert dto.business_definition is None  # 未设置

    def test_update_invalid_status_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            KpiCatalogUpdate(status="BOGUS")

    def test_update_clears_field_with_explicit_none(self) -> None:
        """null 表示显式清空（与 Phase 3.4 object_type 同模式）。"""
        dto = KpiCatalogUpdate(owner=None, unit=None)
        dumped = dto.model_dump(exclude_unset=True)
        assert "owner" in dumped
        assert dumped["owner"] is None
        assert dumped["unit"] is None

    def test_read_dto_carries_all_fields(self) -> None:
        from datetime import datetime

        read = KpiCatalogRead(
            id=1,
            kpiCode="KPI_X",
            kpiName="X",
            status="DRAFT",
            version="v1.0",
            revisionCount=0,
            createdTime=datetime(2026, 1, 1),
            updatedTime=datetime(2026, 1, 2),
        )
        assert read.id == 1
        assert read.status == KpiStatus.DRAFT

    def test_model_dump_json_by_alias_camelcase(self) -> None:
        dto = KpiCatalogCreate(kpiCode="K1", kpiName="N")
        dumped = dto.model_dump(mode="json", by_alias=True)
        assert "kpiCode" in dumped
        assert "kpiName" in dumped
        assert dumped["status"] == "DRAFT"
