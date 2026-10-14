"""F5 RED test: 动作词从追问关键词集拆分 + 有历史短消息不生成盲 SQL。

5 个断言：
1. 动作词「汇总一下今年采购」不再触发 CLARIFY（带完整主体本就是合法查询）
2. 纯指代词「那去年呢」仍然触发 CLARIFY（无历史）和 REFINE/FOLLOW_UP（有历史）
3. 短消息「查」有历史时走 CLARIFY，不是 NEW_QUERY（修复盲 SQL 洞）
4. 短消息「查」无历史时仍走 CHITCHAT（既有行为不变）
5. 含「按月」的长查询不会被误判为 CLARIFY（关键词收窄不放跑正常查询）

Frozen matrix 修正：
- 行 134：「那去年呢」无状态列 → CLARIFY（原值 QUERY 是 pre-P0-2 过期冻结，
  P0-2 step 8 注释明写「短追问 → CLARIFY 询问比较对象，不生成猜测 SQL」）
- 行 40：「查」有状态列 → CLARIFY（原值 CHITCHAT 是真空洞，本任务修）
"""

from __future__ import annotations

import pytest

from app.domain.enums import IntentType
from app.services.intent_service import IntentService


class TestShortFollowupKeywordSplit:
    """动作词移除后，纯指代词仍正确触发 CLARIFY/REFINE/FOLLOW_UP。"""

    @pytest.fixture()
    def svc(self) -> IntentService:
        return IntentService()

    # 1. 动作词不再触发 CLARIFY
    def test_action_word_with_subject_not_clarify(
        self, svc: IntentService
    ) -> None:
        """「汇总一下今年采购」是完整查询，不是追问，不该触发 CLARIFY。"""
        result = svc.classifyResult("汇总一下今年采购", hasPriorState=False)
        assert result.intent != IntentType.CLARIFY, (
            "动作词带完整主体不应判 CLARIFY"
        )

    # 2. 纯指代词仍然触发 CLARIFY（无历史）和 REFINE/FOLLOW_UP（有历史）
    def test_pure_pronoun_no_state_clarify(
        self, svc: IntentService
    ) -> None:
        """「那去年呢」无历史 → CLARIFY（询问比较对象，不生成猜测 SQL）。"""
        result = svc.classifyResult("那去年呢", hasPriorState=False)
        assert result.intent == IntentType.CLARIFY, (
            f"期望 CLARIFY，实际 {result.intent}"
        )

    def test_pure_pronoun_with_state_follow_up(
        self, svc: IntentService
    ) -> None:
        """「那去年呢」有历史 → FOLLOW_UP（追问上一轮结果）。"""
        result = svc.classifyResult("那去年呢", hasPriorState=True)
        assert result.intent == IntentType.FOLLOW_UP, (
            f"期望 FOLLOW_UP，实际 {result.intent}"
        )

    # 3. 短消息 + 有历史 → CLARIFY（修复盲 SQL 洞）
    def test_short_message_with_state_clarify(
        self, svc: IntentService
    ) -> None:
        """「查」有历史时走 CLARIFY，不是 NEW_QUERY 生成盲 SQL。"""
        result = svc.classifyResult("查", hasPriorState=True)
        assert result.intent == IntentType.CLARIFY, (
            f"期望 CLARIFY（不生成盲 SQL），实际 {result.intent}"
        )

    # 4. 短消息 + 无历史 → 仍 CHITCHAT（既有行为不变）
    def test_short_message_no_state_chitchat(
        self, svc: IntentService
    ) -> None:
        """「查」无历史时仍走 CHITCHAT（保持既有行为）。"""
        result = svc.classifyResult("查", hasPriorState=False)
        assert result.intent == IntentType.CHITCHAT, (
            f"期望 CHITCHAT，实际 {result.intent}"
        )

    # 5. 反向断言：含动作词的长查询不会被误判为 CLARIFY
    def test_long_query_with_action_word_not_clarify(
        self, svc: IntentService
    ) -> None:
        """「按月汇总各供应商的销售额总额」是正常查询，不应误判 CLARIFY。"""
        result = svc.classifyResult(
            "按月汇总各供应商的销售额总额", hasPriorState=False
        )
        assert result.intent != IntentType.CLARIFY, (
            "含动作词的长查询不应触发 CLARIFY"
        )
