"""本体属性 business_aliases/description 字段边界校验（纯 pydantic，无 IO）测试。

覆盖：别名数量/单项长度封顶、描述长度封顶、未提供别名放行。
触 DB 的 CRUD 持久化测试已迁至 integration/test_ontology_property_service.py
（【迁移：真实 PG】第三批）。
"""

from __future__ import annotations

import pytest
import pydantic

from app.domain.schemas import OntologyPropertyCreate


class TestBoundaryValidation:
    def test_business_aliases_rejects_oversized_item(self) -> None:
        """单项超过 100 字符被拒绝，防止 schema 文本被本体配置撑爆。"""
        with pytest.raises(pydantic.ValidationError):
            OntologyPropertyCreate(
                class_id=1, property_name="X", data_type="STRING",
                business_aliases=["a" * 101],
            )

    def test_business_aliases_rejects_too_many_items(self) -> None:
        """别名超过 20 项被拒绝。"""
        with pytest.raises(pydantic.ValidationError):
            OntologyPropertyCreate(
                class_id=1, property_name="X", data_type="STRING",
                business_aliases=[f"a{i}" for i in range(21)],
            )

    def test_description_rejects_oversized(self) -> None:
        """描述超过 500 字符被拒绝。"""
        with pytest.raises(pydantic.ValidationError):
            OntologyPropertyCreate(
                class_id=1, property_name="X", data_type="STRING",
                description="x" * 501,
            )

    def test_business_aliases_none_is_allowed(self) -> None:
        """未提供别名是合法场景（None 放行）。"""
        dto = OntologyPropertyCreate(class_id=1, property_name="X", data_type="STRING")
        assert dto.business_aliases is None
