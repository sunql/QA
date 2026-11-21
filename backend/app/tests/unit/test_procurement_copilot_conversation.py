"""多轮 Copilot 的上下文构建（纯函数，无 DB）。

判据要点：
1. 无历史 ⇒ **原样返回**（run.question 保持干净，别给首轮加壳）；
2. 有历史 ⇒ `[对话上下文]` + `[当前问题]`，且**当前问题在最后**
   （planner 读到最后一句才知道用户现在要什么）；
3. 超长 ⇒ 保新弃旧（旧的更可能已被后续结论覆盖），不是截断当前问题；
4. 空白/换行归一（上下文块要可读，不能把用户多行原样塞进去撑爆预算）。
"""

from __future__ import annotations

from app.services.procurement_copilot_conversation import (
    CONTEXT_HEADER,
    CURRENT_QUESTION_HEADER,
    TurnHistory,
    buildContextBlock,
    buildRunInput,
)


def _turn(question: str, answer: str | None = "好的", status: str = "SUCCEEDED") -> TurnHistory:
    return TurnHistory(question=question, answer=answer, status=status)


def test_no_history_returns_message_unchanged() -> None:
    assert buildRunInput("找一下不锈钢板", []) == "找一下不锈钢板"
    assert buildContextBlock([]) == ""


def test_history_block_puts_current_question_last() -> None:
    text = buildRunInput("那第二家呢？", [_turn("不锈钢板有哪些供应商", "共 4 家")])

    assert text.startswith(CONTEXT_HEADER)
    assert "不锈钢板有哪些供应商" in text
    assert "共 4 家" in text
    assert text.index(CURRENT_QUESTION_HEADER) < text.index("那第二家呢？")
    assert text.endswith("那第二家呢？")


def test_only_last_n_turns_are_used() -> None:
    history = [_turn(f"问题{i}", f"答案{i}") for i in range(6)]

    text = buildContextBlock(history, maxTurns=2)

    assert "问题5" in text and "问题4" in text
    assert "问题3" not in text


def test_oldest_is_dropped_first_when_over_budget() -> None:
    long_answer = "很长的答案" * 100
    history = [_turn("旧问题", long_answer), _turn("新问题", "新答案")]

    text = buildContextBlock(history, maxChars=120)

    assert "新问题" in text
    assert "旧问题" not in text


def test_unfinished_turn_is_labelled() -> None:
    text = buildContextBlock([_turn("找物料", None, status="PAUSED")])

    assert "未完成" in text


def test_whitespace_is_collapsed() -> None:
    text = buildContextBlock([_turn("多行\n问题\n 带空格  ", "回答\n\n第二段")])

    assert "\n\n" not in text.split(CONTEXT_HEADER)[1].strip("\n").split("\n")[1]
    assert "多行 问题 带空格" in text
