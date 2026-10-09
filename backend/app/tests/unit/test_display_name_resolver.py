"""显示名解析服务：列名/值 → 中文标签。

真实 PG 测试：测试 OntologyProperty 命中 + 缺失 + value dict 命中 + fallback。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty
from app.services.display_name_resolver import (
    DISPLAY_VALUE_DICT,
    resolveColumnName,
    resolveValueName,
)


@pytest.mark.asyncio
async def test_resolveColumnName_returns_alias_when_found(db_session: AsyncSession) -> None:
    """seed OntologyClass(source_table=foo) + OntologyProperty(source_column=bar, alias='基地代码')
    → resolveColumnName returns '基地代码'.

    注：实际 ORM 模型里 `source_table` 在 `OntologyClass` 上，`OntologyProperty` 通过
    `class_id` 外键引用类。seed 时先落 class 再落 property。
    """
    cls = OntologyClass(class_name="foo", source_table="foo")
    db_session.add(cls)
    await db_session.flush()

    db_session.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="bar",
            data_type="string",
            source_column="bar",
            property_alias="基地代码",
        )
    )
    await db_session.commit()

    result = await resolveColumnName(db_session, "foo", "bar")
    assert result == "基地代码"


@pytest.mark.asyncio
async def test_resolveColumnName_falls_back_to_column_name(db_session: AsyncSession) -> None:
    """无 property 行 → 返原 column 名."""
    result = await resolveColumnName(db_session, "foo", "unknown_col")
    assert result == "unknown_col"


def test_resolveValueName_returns_dict_value() -> None:
    """DISPLAY_VALUE_DICT 命中：'QTY' → '数量确认'."""
    result = resolveValueName("dwd_reconciliation_bill_dtl", "confirm_stage", "QTY")
    assert result == "数量确认"


def test_resolveValueName_falls_back_to_str() -> None:
    """未命中 + None + 缺 sourceTable/Column 全部 fallback."""
    # 未命中：返原 value 字符串
    assert resolveValueName("dwd_reconciliation_bill_dtl", "site_code", "C1") == "C1"
    # None → "—"
    assert resolveValueName(None, None, None) == "—"
    # 缺 sourceTable → 返原 value
    assert resolveValueName(None, "confirm_stage", "QTY") == "QTY"
    # 缺 sourceColumn → 返原 value
    assert resolveValueName("dwd_reconciliation_bill_dtl", None, "QTY") == "QTY"