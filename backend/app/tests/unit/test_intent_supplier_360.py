"""Supplier 360 chat 意图识别单测（Phase 5.3）。

覆盖 intent_service._extractSupplierKey 的边界：
- 命中多种问法 → SUPPLIER_360 + supplierKey
- 不命中（普通供应商查询）→ 不走 SUPPLIER_360（保持 QUERY / CHITCHAT 现有路径）
- 5-9 位数字限制避免误中日期
"""

from __future__ import annotations

import pytest

from app.domain.enums import IntentType
from app.services.intent_service import IntentService


@pytest.fixture()
def svc() -> IntentService:
    return IntentService()


@pytest.mark.parametrize(
    "question,expected_key",
    [
        # 标准问法
        ("供应商 100001 的 360° 视图", "100001"),
        ("供应商 100001 的 360 视图", "100001"),
        ("供应商 100001 的 360", "100001"),
        # supplier + 全貌
        ("supplier 100001 全貌", "100001"),
        # 顺序调换
        ("360 视图 供应商 100001", "100001"),
        # 9 位 enterprise_key
        ("供应商 999999999 的 360° 视图", "999999999"),
    ],
)
def test_supplier_360_patterns_match(svc: IntentService, question, expected_key):
    result = svc.classifyResult(question)
    assert result.intent == IntentType.SUPPLIER_360
    assert result.supplierKey == expected_key


@pytest.mark.parametrize(
    "question",
    [
        # 普通供应商查询（无 360 / 全貌）→ 不应被吸到 supplier_360
        "供应商 100001 的订单数",
        "查询供应商 100001 的收货记录",
        "供应商 100001 的质量数据",
        # 无 enterprise_key
        "供应商的 360 度全景",
        "供应商全貌",
    ],
)
def test_non_supplier_360_questions_not_misclassified(svc: IntentService, question):
    """关键反例：避免 supplier-360 拦截把普通查询吸走。"""
    result = svc.classifyResult(question)
    assert result.intent != IntentType.SUPPLIER_360, (
        f"问句「{question}」不应被识别为 supplier_360"
    )


def test_supplier_360_priority_over_query(svc: IntentService):
    """含 "供应商 X 的订单" + "360" 时，supplier_360 应优先于 QUERY。"""
    result = svc.classifyResult("供应商 100001 的 360 度订单分布")
    assert result.intent == IntentType.SUPPLIER_360
    assert result.supplierKey == "100001"


def test_short_message_does_not_trigger_supplier_360(svc: IntentService):
    """过短消息走 CHITCHAT，不进 supplier_360。"""
    result = svc.classifyResult("360")
    assert result.intent != IntentType.SUPPLIER_360
