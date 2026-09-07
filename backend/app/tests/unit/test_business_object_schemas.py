"""BusinessObjectCode 字面量类型 + Pydantic DTO 校验."""
from typing import get_args

import pytest
from pydantic import ValidationError

from app.domain.enums import BusinessObjectCode
from app.domain.schemas import (
    BusinessObjectCreate,
    BusinessObjectRead,
    BusinessObjectUpdate,
)


def test_business_object_code_has_six_values() -> None:
    assert sorted(get_args(BusinessObjectCode)) == [
        "GR",
        "IQC",
        "MATERIAL",
        "NCR",
        "PO",
        "SUPPLIER",
    ]


def test_create_accepts_valid_code() -> None:
    dto = BusinessObjectCreate(
        code="SUPPLIER", name="供应商", graph_label="Supplier"
    )
    assert dto.code == "SUPPLIER"
    assert dto.graph_label == "Supplier"


def test_create_rejects_invalid_code() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="UNKNOWN", name="X")  # type: ignore[arg-type]


def test_create_rejects_oversized_name() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="SUPPLIER", name="x" * 101)


def test_create_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="SUPPLIER", name="")


def test_update_partial_fields() -> None:
    dto = BusinessObjectUpdate(name="新名字")
    assert dto.name == "新名字"
    assert dto.graph_label is None


def test_update_explicit_none_clears_graph_label() -> None:
    """显式 None 视为清空字段（与 Phase 4.1 object_type 同模式）."""
    dto = BusinessObjectUpdate(graph_label=None)
    assert dto.graph_label is None


def test_description_max_length_4000() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="X", description="x" * 4001
        )


def test_read_carries_all_fields() -> None:
    dto = BusinessObjectRead(
        code="PO",
        name="采购订单",
        header_class_id=5,
        graph_label="PurchaseOrder",
        description="...",
    )
    assert dto.code == "PO"
    assert dto.header_class_id == 5
