"""BusinessObjectCode 字面量类型 + Pydantic DTO 校验.

feat-data-domain-layer：header_class_id / primary_domain_id 成为 Create 必填
（0133 收紧 NOT NULL + 有且只有一个主域），Create 载荷需带全字段。
"""
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
        code="SUPPLIER",
        name="供应商",
        header_class_id=1,
        primary_domain_id="PROCUREMENT",
        graph_label="Supplier",
    )
    assert dto.code == "SUPPLIER"
    assert dto.graph_label == "Supplier"
    assert dto.primary_domain_id == "PROCUREMENT"
    assert dto.attached_domain_ids == []


def test_create_rejects_missing_header_or_domain() -> None:
    """0133 后 header_class_id / primary_domain_id 必填：缺失 → 拒。"""
    with pytest.raises(ValidationError):
        BusinessObjectCreate(code="SUPPLIER", name="X")
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="X", header_class_id=1
        )
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="X", primary_domain_id="PROCUREMENT"
        )


def test_create_rejects_invalid_code() -> None:
    """BusinessObjectCodeNew 为 format-only 校验（feat-business-object-dynamic）：
    registry 检查交给 DB 约束——「存在性未知」的 code 允许创建，只有格式非法才拒。"""
    # 小写 → 拒
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="unknown", name="X", header_class_id=1,
            primary_domain_id="PROCUREMENT",
        )
    # 含空格/特殊字符 → 拒
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="HAS SPACE", name="X", header_class_id=1,
            primary_domain_id="PROCUREMENT",
        )
    # 超 20 字符 → 拒
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="X" * 21, name="X", header_class_id=1,
            primary_domain_id="PROCUREMENT",
        )
    # 格式合法但 registry 不存在 → 允许（创建语义本身）
    dto = BusinessObjectCreate(
        code="UNKNOWN", name="X", header_class_id=1,
        primary_domain_id="PROCUREMENT",
    )
    assert dto.code == "UNKNOWN"


def test_create_rejects_unknown_domain() -> None:
    """primary_domain_id 走 registry 校验（enabled 域），未知码 → 拒。"""
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="X", header_class_id=1,
            primary_domain_id="NOT_A_DOMAIN",
        )
    # 附域同样校验
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="X", header_class_id=1,
            primary_domain_id="PROCUREMENT",
            attached_domain_ids=["NOT_A_DOMAIN"],
        )


def test_create_rejects_oversized_name() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="x" * 101, header_class_id=1,
            primary_domain_id="PROCUREMENT",
        )


def test_create_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        BusinessObjectCreate(
            code="SUPPLIER", name="", header_class_id=1,
            primary_domain_id="PROCUREMENT",
        )


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
            code="SUPPLIER", name="X", description="x" * 4001,
            header_class_id=1, primary_domain_id="PROCUREMENT",
        )


def test_read_carries_all_fields() -> None:
    dto = BusinessObjectRead(
        code="PO",
        name="采购订单",
        header_class_id=5,
        primary_domain_id="PROCUREMENT",
        attached_domain_ids=["LOGISTICS"],
        graph_label="PurchaseOrder",
        description="...",
    )
    assert dto.code == "PO"
    assert dto.header_class_id == 5
    assert dto.primary_domain_id == "PROCUREMENT"
    assert dto.attached_domain_ids == ["LOGISTICS"]
