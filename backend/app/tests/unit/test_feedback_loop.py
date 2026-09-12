"""机制 1 反馈三态判定（纯函数）单元测试（feat-wiki-knowledge M3）。

判定规则是学习闭环的口径定义，必须能被单独钉住：三态是一组**全序划分**
（任何一次处置恰好命中一个），口径漂了会同时污染训练数据与运营统计。
"""

from __future__ import annotations

import pytest

from app.services.learning.feedback_loop import decideClassificationAction

_SUGGESTION = {"primary": "RULE", "confidence": 0.9}


@pytest.mark.parametrize(
    ("suggestion", "newDimension", "expected"),
    [
        # 与建议一致 → 确认
        (_SUGGESTION, "RULE", "CONFIRM"),
        # 改成别的维度 → 修正
        (_SUGGESTION, "PROCESS", "MODIFY"),
        # 清空维度 → 打回
        (_SUGGESTION, None, "REJECT"),
        # 无建议（未分类过）→ 不构成反馈
        (None, "RULE", None),
        (None, None, None),
        # 建议结构损坏（缺 primary / 类型不对）→ 同样不构成反馈
        ({}, "RULE", None),
        ({"primary": None}, "RULE", None),
        ({"primary": 123}, "RULE", None),
        ({"primary": ""}, "RULE", None),
    ],
)
def test_decide_classification_action(
    suggestion: dict | None, newDimension: str | None, expected: str | None
) -> None:
    assert decideClassificationAction(suggestion, newDimension) == expected


def test_reject_takes_precedence_over_primary_comparison() -> None:
    """建议 primary 为空串时清空维度仍算「无建议」，而不是 REJECT。

    这条防的是「无建议」被误判成「用户打回了某条建议」——打回的前提是
    确实存在一条可被打回的建议。
    """
    assert decideClassificationAction({"primary": ""}, None) is None
