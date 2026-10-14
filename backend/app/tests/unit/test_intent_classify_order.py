"""intent_service 分类顺序与礼貌前缀剥除的单元测试。

RED: 现顺序 CHITCHAT_KEYWORDS / 长度 <= 3 在最前，会短路所有追问与前缀判断。
"""
from __future__ import annotations

import pytest

from app.services.intent_service import IntentService


@pytest.fixture
def service() -> IntentService:
    return IntentService()


def test_short_followup_year_returns_refine_not_chitchat(service: IntentService):
    """hasPriorState=True + '去年呢' -> REFINE 而非 CHITCHAT。"""
    result = service.classifyResult("去年呢", hasPriorState=True)
    assert result.intent.value in ("refine", "follow_up"), f"got {result.intent}"


def test_short_followup_compare_returns_refine(service: IntentService):
    """hasPriorState=True + '同比' -> REFINE。"""
    result = service.classifyResult("同比", hasPriorState=True)
    assert result.intent.value in ("refine", "follow_up"), f"got {result.intent}"


def test_short_followup_aggregate_returns_refine(service: IntentService):
    """hasPriorState=True + '汇总' -> REFINE。"""
    result = service.classifyResult("汇总", hasPriorState=True)
    assert result.intent.value in ("refine", "follow_up"), f"got {result.intent}"


def test_greeting_prefix_stripped_then_query(service: IntentService):
    """'你好，请查询 2025 采购总额' -> QUERY 而非 CHITCHAT（剥前缀后是业务查询）。"""
    result = service.classifyResult("你好，请查询 2025 采购总额", hasPriorState=False)
    assert result.intent.value not in ("chitchat",), f"got {result.intent}"


def test_pure_thanks_remains_chitchat(service: IntentService):
    """'谢谢' -> 仍然 CHITCHAT（0 LLM）。"""
    result = service.classifyResult("谢谢", hasPriorState=False)
    assert result.intent.value == "chitchat", f"got {result.intent}"


def test_compare_without_history_returns_clarify(service: IntentService):
    """hasPriorState=False + '同比' -> CLARIFY（询问比较对象），不直接生成猜测 SQL。"""
    result = service.classifyResult("同比", hasPriorState=False)
    assert result.intent.value == "clarify", f"got {result.intent}"


def test_aggregate_keyword_in_long_query_not_clarify(service: IntentService):
    """'各供应商的收货数量汇总' (10 chars) -> QUERY 而非 CLARIFY。

    跟踪 length guard 阈值的边界 case：阈值 9 改 10 任何一边都需重新审视。
    """
    result = service.classifyResult("各供应商的收货数量汇总", hasPriorState=False)
    assert result.intent.value == "query", f"got {result.intent}"
