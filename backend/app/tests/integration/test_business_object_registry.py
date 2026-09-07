"""BusinessObjectRegistry warmUp / reloadOne / isValid 集成测试。

真实 PostgreSQL（qa_metadata_test）+ 完整 API 链路。
"""
from __future__ import annotations

import pytest

from app.domain.models import BusinessObject
from app.services.business_object_registry import businessObjectRegistry


@pytest.mark.asyncio
async def test_isValid_after_warmUp_returns_true_for_seeded_codes(dbSession) -> None:
    """warmUp 后，seed 过的 6 个 code 均 valid。"""
    await businessObjectRegistry.warmUp(dbSession)
    for code in ["SUPPLIER", "MATERIAL", "PO", "GR", "IQC", "NCR"]:
        assert businessObjectRegistry.isValid(code), f"{code} should be valid"


@pytest.mark.asyncio
async def test_isValid_returns_false_for_unknown_code(dbSession) -> None:
    """未知 code 返回 False。"""
    await businessObjectRegistry.warmUp(dbSession)
    assert businessObjectRegistry.isValid("INVOICE") is False
    assert businessObjectRegistry.isValid("ZZZ") is False


@pytest.mark.asyncio
async def test_getAll_returns_frozenset(dbSession) -> None:
    """getAll 返回 frozenset，且包含已知 code。"""
    await businessObjectRegistry.warmUp(dbSession)
    codes = businessObjectRegistry.getAll()
    assert isinstance(codes, frozenset)
    assert "SUPPLIER" in codes


@pytest.mark.asyncio
async def test_reloadOne_adds_new_code(dbSession) -> None:
    """插入新 business_object 行后 reloadOne，isValid 对新 code 返回 True。"""
    await businessObjectRegistry.warmUp(dbSession)
    # 直接插一行（绕过 service 层）
    new_code = "INVOICE"
    new_obj = BusinessObject(
        code=new_code,
        name="发票",
        header_class_id=None,
        graph_label=None,
        description="Test invoice object",
        created_by="test",
    )
    dbSession.add(new_obj)
    await dbSession.commit()
    await businessObjectRegistry.reloadOne(dbSession, new_code)
    assert businessObjectRegistry.isValid(new_code) is True


@pytest.mark.asyncio
async def test_reloadOne_removes_deleted_code(dbSession) -> None:
    """删除 business_object 行后 reloadOne，isValid 对已删 code 返回 False。"""
    await businessObjectRegistry.warmUp(dbSession)
    # 先插再删（确保存在）
    new_code = "INVOICE"
    new_obj = BusinessObject(
        code=new_code,
        name="发票",
        header_class_id=None,
        graph_label=None,
        description="Test invoice object",
        created_by="test",
    )
    dbSession.add(new_obj)
    await dbSession.commit()
    await businessObjectRegistry.reloadOne(dbSession, new_code)
    assert businessObjectRegistry.isValid(new_code) is True
    # 删除
    row = await dbSession.get(BusinessObject, new_code)
    assert row is not None
    await dbSession.delete(row)
    await dbSession.commit()
    await businessObjectRegistry.reloadOne(dbSession, new_code)
    assert businessObjectRegistry.isValid(new_code) is False


def test_isValid_raises_runtime_error_before_warmUp() -> None:
    """未 warmUp 前调用 isValid 抛 RuntimeError。"""
    businessObjectRegistry.invalidate()
    with pytest.raises(RuntimeError, match="未 warmUp"):
        businessObjectRegistry.isValid("SUPPLIER")
