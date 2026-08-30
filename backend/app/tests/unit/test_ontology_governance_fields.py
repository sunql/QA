"""Phase 3.4 本体治理字段单元测试（RED）。

OntologyClass 新增 2 个治理字段，满足采购域 Sheet 03 业务对象目录：
- object_type：Master（主数据）/ Transaction（交易单据）/ Reference（参考/配置）/ Event（事件）
- object_owner：责任部门/人

覆盖契约：
- ObjectType 枚举四类取值（与 DB VARCHAR(20) 兼容）
- Create / Update / Read DTO 携带治理字段 roundtrip + 非法 object_type 拒绝
- seed CLASSES 全部 27 类治理字段齐备 + 分类映射抽查
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.enums import ObjectType
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyClassRead,
    OntologyClassUpdate,
)
from seed_ontology import CLASSES


class TestObjectTypeEnum:
    def test_object_type_has_four_categories(self) -> None:
        """ObjectType 恰含 Master/Transaction/Reference/Event 四类。"""
        assert {m.value for m in ObjectType} == {
            "Master",
            "Transaction",
            "Reference",
            "Event",
        }

    def test_object_type_values_fit_column(self) -> None:
        """取值 ≤20 字符（DB VARCHAR(20)）。"""
        for member in ObjectType:
            assert isinstance(member.value, str)
            assert len(member.value) <= 20


class TestClassSchemasGovernance:
    def test_create_accepts_governance_fields(self) -> None:
        dto = OntologyClassCreate(
            class_name="Order",
            object_type=ObjectType.TRANSACTION,
            object_owner="采购部",
        )
        assert dto.object_type == ObjectType.TRANSACTION
        assert dto.object_owner == "采购部"

    def test_create_governance_fields_optional(self) -> None:
        dto = OntologyClassCreate(class_name="Order")
        assert dto.object_type is None
        assert dto.object_owner is None

    def test_create_rejects_invalid_object_type(self) -> None:
        with pytest.raises(ValidationError):
            OntologyClassCreate(class_name="X", object_type="Nonsense")  # type: ignore[arg-type]

    def test_update_accepts_governance_fields(self) -> None:
        dto = OntologyClassUpdate(
            object_type=ObjectType.MASTER, object_owner="主数据管理组"
        )
        assert dto.object_type == ObjectType.MASTER
        assert dto.object_owner == "主数据管理组"

    def test_read_includes_governance_fields(self) -> None:
        data = OntologyClassRead(
            id=1,
            class_name="Order",
            object_type=ObjectType.TRANSACTION,
            object_owner="采购部",
        )
        assert data.object_type == ObjectType.TRANSACTION
        assert data.object_owner == "采购部"
        # JSON 序列化输出字符串值（非枚举成员名）+ camelCase 别名（FastAPI 默认 by_alias）
        assert data.model_dump(mode="json", by_alias=True)["objectType"] == "Transaction"


class TestSeedGovernanceMapping:
    def test_all_classes_have_valid_governance_fields(self) -> None:
        valid = {m.value for m in ObjectType}
        for c in CLASSES:
            assert c["object_type"] in valid, f"{c['source_table']} object_type 非法"
            assert c["object_owner"] and c["object_owner"].strip(), (
                f"{c['source_table']} object_owner 为空"
            )

    def test_master_class_mapping(self) -> None:
        byTable = {c["source_table"]: c for c in CLASSES}
        assert byTable["ITMMASTER"]["object_type"] == "Master"
        assert byTable["BPSUPPLIER"]["object_type"] == "Master"
        assert byTable["FACILITY"]["object_type"] == "Master"

    def test_transaction_class_mapping(self) -> None:
        byTable = {c["source_table"]: c for c in CLASSES}
        assert byTable["PORDER"]["object_type"] == "Transaction"
        assert byTable["PORDERQ"]["object_type"] == "Transaction"
        assert byTable["PINVOICE"]["object_type"] == "Transaction"
        assert byTable["PAYMENTH"]["object_type"] == "Transaction"
        assert byTable["PQUOTAT"]["object_type"] == "Transaction"

    def test_reference_class_mapping(self) -> None:
        byTable = {c["source_table"]: c for c in CLASSES}
        assert byTable["PPRICCONF"]["object_type"] == "Reference"
        assert byTable["PREQUISO"]["object_type"] == "Reference"

    def test_governance_owner_assigned_by_type(self) -> None:
        """Master 类 owner=主数据管理组；交易/参考类 owner=采购部。"""
        for c in CLASSES:
            if c["object_type"] == "Master":
                assert c["object_owner"] == "主数据管理组", c["source_table"]
            else:
                assert c["object_owner"] == "采购部", c["source_table"]
