"""Test that 6 chat UI-copy constants exist in messages_zh.py SSOT.

Run with: pytest backend/app/tests/unit/services/test_chat_ui_copy.py -x -q
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="requires TEST_DATABASE_URL (DB fixture environment)",
)


def test_chat_ui_copy_in_messages_zh():
    # These must be importable from the SSOT; if the module is missing a constant
    # this raises ImportError and the test fails (RED).
    from app.services.messages_zh import (
        MSG_CHAT_UNANSWERABLE_NO_DATA,
        MSG_CHAT_UNANSWERABLE_MISSING_VECTOR,
        MSG_CHAT_STEP_GEN_FAILED_PREFIX,
        MSG_CHAT_STEP_EXEC_FAILED_PREFIX,
        MSG_CHAT_STEP_UNANSWERABLE,
        MSG_CHAT_STEP_AGGREGATION_SKIPPED,
    )

    # Spot-check the string values match the hardcoded originals
    assert "抱歉，当前系统中没有与您的问题相关的业务数据" in MSG_CHAT_UNANSWERABLE_NO_DATA
    assert "向量同步" in MSG_CHAT_UNANSWERABLE_MISSING_VECTOR
    assert MSG_CHAT_STEP_GEN_FAILED_PREFIX == "该步骤查询生成失败："
    assert MSG_CHAT_STEP_EXEC_FAILED_PREFIX == "该步骤执行失败："
    assert MSG_CHAT_STEP_UNANSWERABLE == "无法回答（LLM 判定无有效查询计划）"
    assert MSG_CHAT_STEP_AGGREGATION_SKIPPED == "未执行（前置数据步骤全部失败）"
