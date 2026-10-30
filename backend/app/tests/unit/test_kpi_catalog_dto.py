"""Pydantic 内存校验 — U1-U3（per F2 spec §5.1）。"""
from __future__ import annotations
import pytest
from pydantic import ValidationError
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate


def test_kpi_create_with_valid_datasource_id_passes() -> None:
    """U1: datasource_id=5 合法。"""
    dto = KpiCatalogCreate(
        kpi_code="K001", kpi_name="test", business_definition="d",
        formula="SUM(x)", numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o", version="1",
        metric_id=None, created_by="u",
        semantic_keywords=[], match_threshold=0.5,
        datasource_id=5,  # 关键
    )
    assert dto.datasource_id == 5


def test_kpi_create_with_zero_datasource_id_rejected() -> None:
    """U2: datasource_id=0 拒绝（ge=1）。"""
    with pytest.raises(ValidationError) as exc_info:
        KpiCatalogCreate(
            kpi_code="K001", kpi_name="test", business_definition="d",
            formula="SUM(x)", numerator="x", denominator=None, grain="g",
            unit="u", data_source="legacy_text", owner="o", version="1",
            metric_id=None, created_by="u",
            semantic_keywords=[], match_threshold=0.5,
            datasource_id=0,
        )
    assert "datasource_id" in str(exc_info.value)


def test_kpi_create_with_negative_datasource_id_rejected() -> None:
    """U3: datasource_id=-1 拒绝（ge=1）。"""
    with pytest.raises(ValidationError) as exc_info:
        KpiCatalogCreate(
            kpi_code="K001", kpi_name="test", business_definition="d",
            formula="SUM(x)", numerator="x", denominator=None, grain="g",
            unit="u", data_source="legacy_text", owner="o", version="1",
            metric_id=None, created_by="u",
            semantic_keywords=[], match_threshold=0.5,
            datasource_id=-1,
        )
    assert "datasource_id" in str(exc_info.value)