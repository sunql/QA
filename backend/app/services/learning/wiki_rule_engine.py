"""业务规则求值器 + dry-run（feat-wiki-knowledge，Phase 8 M6）。

**纯函数**：不碰 DB、不碰 LLM、不读时钟。规则的求值结果只由
``rule_expression`` 与传入的 ``record`` 决定。这条约束是刻意的 —— 规则会
被 Agent 在生产链路里直读执行，求值器一旦有副作用或隐式取数，「为什么这条
规则当时判了命中」就再也复现不出来。

## 三态而非两态（本模块最重要的设计）

每个条件的结果是 ``True`` / ``False`` / ``None``：

- ``True``  —— 记录里取到了值，且比较成立
- ``False`` —— 记录里取到了值，但比较不成立
- ``None``  —— **判不了**：记录里没有这个字段，或值不可比（拿「很多」去比 ≥1000）

把 ``None`` 归并进 ``False`` 是这类系统最典型的隐性事故：一条规则在生产里
对每条缺字段的记录静默不触发，而 dry-run 全绿 —— 因为样例恰好都带了字段。
故 ``RuleEvaluation.matched`` 用 ``is True`` 判定（``None`` 自然为假，**不可
命中**），同时把 ``undecidable`` 单列一态供调用方显式处置。

## 算子

M5 只把算子归一化写进了 prompt，代码里并未强制（``_validateRule`` 仅 strip），
所以库里可能存着「不低于」。求值前一律经 ``normalizeOperator``，白名单之外的
算子**抛 422**而不是判否 —— 判否会让一条写错算子的规则静默永不触发。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.exceptions import ValidationError
from app.domain.wiki_learning_models import RULE_OPERATORS
from app.services.messages_zh import (
    MSG_WIKI_RULE_CONDITIONS_REQUIRED,
    MSG_WIKI_RULE_EXAMPLE_INVALID,
    MSG_WIKI_RULE_OPERATOR_UNSUPPORTED,
)

logger = logging.getLogger(__name__)

# dry-run 单例结果的三态
PASSED = "PASSED"
FAILED = "FAILED"
UNDECIDABLE = "UNDECIDABLE"

# 比较类型：需要两侧都能转成数值才能判
_ORDER_OPERATORS = frozenset({">=", "<=", ">", "<"})
_EQUALITY_OPERATORS = frozenset({"=", "!="})
_MEMBERSHIP_OPERATORS = frozenset({"IN", "NOT IN"})

# 符号变体：业务正文里 ≥ / ≤ 是常态，直接抄进算子栏的概率很高
_SYMBOL_ALIASES: dict[str, str] = {
    "≥": ">=",
    "≤": "<=",
    "＞": ">",
    "＜": "<",
    "＝": "=",
    "≠": "!=",
    "==": "=",
    "≥=": ">=",
    "≤=": "<=",
}

# 中文表述 → 算子。业务文档写「注册资本不低于 1000 万元」，模型照抄这个说法
# 回来是完全可能的。
_WORD_ALIASES: dict[str, str] = {
    "不低于": ">=",
    "不少于": ">=",
    "不小于": ">=",
    "至少": ">=",
    "不高于": "<=",
    "不超过": "<=",
    "不大于": "<=",
    "至多": "<=",
    "大于": ">",
    "超过": ">",
    "多于": ">",
    "高于": ">",
    "小于": "<",
    "少于": "<",
    "低于": "<",
    "等于": "=",
    "为": "=",
    "不等于": "!=",
    "不为": "!=",
    "不是": "!=",
    "属于": "IN",
    "不属于": "NOT IN",
}


def normalizeOperator(raw: Any) -> str | None:
    """把算子归一化成 ``RULE_OPERATORS`` 里的写法；认不出来返回 ``None``。

    不抛异常：调用方分两种态度 —— 物化时认不出即拒绝（规则不该带着脏算子落库），
    求值时认不出即 422（已在库里的脏算子必须炸出来，不能静默判否）。把「认不出」
    与「怎么处置」分开，两种态度才都能表达。
    """
    if not isinstance(raw, str):
        return None
    token = raw.strip()
    if not token:
        return None
    token = _SYMBOL_ALIASES.get(token, token)
    if token in RULE_OPERATORS:
        return token
    upper = token.upper()
    if upper in RULE_OPERATORS:  # in / not in 的大小写变体
        return upper
    return _WORD_ALIASES.get(token)


def requireOperator(raw: Any) -> str:
    """归一化，认不出即抛 422（物化与求值两处的共用入口）。"""
    operator = normalizeOperator(raw)
    if operator is None:
        raise ValidationError(
            MSG_WIKI_RULE_OPERATOR_UNSUPPORTED.format(operator=raw)
        )
    return operator


@dataclass(frozen=True)
class ConditionResult:
    """单个条件的求值结果。"""

    field: str
    operator: str
    expected: Any
    actual: Any
    present: bool
    # None = 判不了（字段缺失 / 值不可比），与 False（比了但不成立）严格区分
    matched: bool | None
    undecidableReason: str | None = None


@dataclass(frozen=True)
class RuleEvaluation:
    """一条规则的求值结果。"""

    matched: bool
    action: dict[str, Any]
    conditions: tuple[ConditionResult, ...]

    @property
    def undecidable(self) -> bool:
        """是否至少有一个条件判不了。"""
        return any(c.matched is None for c in self.conditions)


@dataclass(frozen=True)
class DryRunCaseResult:
    """一条样例的 dry-run 结果。"""

    index: int
    input: dict[str, Any]
    expectedMatched: bool
    actualMatched: bool
    status: str
    conditions: tuple[ConditionResult, ...]


@dataclass(frozen=True)
class DryRunReport:
    """dry-run 汇总。计数是**属性**而非字段：多存一份就会有机会与实际结果不一致。"""

    results: tuple[DryRunCaseResult, ...]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == FAILED)

    @property
    def undecidable(self) -> int:
        return sum(1 for r in self.results if r.status == UNDECIDABLE)


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------


def evaluateRule(
    ruleExpression: Mapping[str, Any], record: Mapping[str, Any]
) -> RuleEvaluation:
    """按 ``ruleExpression`` 求值一条记录（条件之间是 **AND**）。

    ``conditions`` 为空即拒绝：一条没有条件的规则会命中**全部**记录，落在
    BLOCK 动作上就是「拦下一切」。空条件多半来自抽取失败，绝不该被当成
    「无条件成立」放行。
    """
    conditions = ruleExpression.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError(MSG_WIKI_RULE_CONDITIONS_REQUIRED)

    results = tuple(_evaluateCondition(raw, record) for raw in conditions)
    action = ruleExpression.get("action")
    return RuleEvaluation(
        # is True 而非真值判断：matched=None（判不了）绝不能算命中
        matched=all(c.matched is True for c in results),
        action=dict(action) if isinstance(action, Mapping) else {},
        conditions=results,
    )


def _evaluateCondition(raw: Any, record: Mapping[str, Any]) -> ConditionResult:
    """求单个条件。形状不合（缺 field/operator）也走「判不了」，不抛。"""
    if not isinstance(raw, Mapping):
        return ConditionResult(
            field="",
            operator="",
            expected=None,
            actual=None,
            present=False,
            matched=None,
            undecidableReason="条件不是对象，无法求值",
        )

    field = raw.get("field")
    fieldName = field.strip() if isinstance(field, str) else ""
    operator = requireOperator(raw.get("operator"))
    expected = raw.get("value")

    if not fieldName:
        return ConditionResult(
            field="",
            operator=operator,
            expected=expected,
            actual=None,
            present=False,
            matched=None,
            undecidableReason="条件缺少 field，无法求值",
        )

    if fieldName not in record:
        return ConditionResult(
            field=fieldName,
            operator=operator,
            expected=expected,
            actual=None,
            present=False,
            matched=None,
            undecidableReason=f"记录里没有字段「{fieldName}」，无法判断",
        )

    actual = record[fieldName]
    matched, reason = _compare(operator, expected, actual)
    return ConditionResult(
        field=fieldName,
        operator=operator,
        expected=expected,
        actual=actual,
        present=True,
        matched=matched,
        undecidableReason=reason,
    )


def _compare(operator: str, expected: Any, actual: Any) -> tuple[bool | None, str | None]:
    """返回 ``(是否成立, 判不了的原因)``。"""
    if operator in _MEMBERSHIP_OPERATORS:
        return _compareMembership(operator, expected, actual)
    if operator in _ORDER_OPERATORS:
        return _compareOrder(operator, expected, actual)
    if operator == "=":
        return _equals(actual, expected), None
    if operator == "!=":
        return not _equals(actual, expected), None
    # requireOperator 已经挡过未知算子，走到这里说明白名单与分派表脱节了
    raise ValidationError(MSG_WIKI_RULE_OPERATOR_UNSUPPORTED.format(operator=operator))


def _compareMembership(
    operator: str, expected: Any, actual: Any
) -> tuple[bool | None, str | None]:
    if not isinstance(expected, list):
        return None, f"算子 {operator} 的取值必须是数组，规则里写的是 {expected!r}"
    isMember = any(_equals(actual, item) for item in expected)
    return (isMember if operator == "IN" else not isMember), None


def _compareOrder(
    operator: str, expected: Any, actual: Any
) -> tuple[bool | None, str | None]:
    left = _toNumber(actual)
    right = _toNumber(expected)
    if left is None:
        return None, f"记录值 {actual!r} 不是数值，算子 {operator} 无法比较"
    if right is None:
        return None, f"规则值 {expected!r} 不是数值，算子 {operator} 无法比较"
    if operator == ">=":
        return left >= right, None
    if operator == "<=":
        return left <= right, None
    if operator == ">":
        return left > right, None
    return left < right, None


def _equals(actual: Any, expected: Any) -> bool:
    """相等判定：两侧都能转数值时按数值比，否则按文本比。

    「按数值比」是必需的：规则值来自 JSONB 里的字符串（M5 的 ``_stringOrNone``
    会把数字统一成字符串），而记录里的值多半是真数字，纯文本比会让 ``"1000"``
    与 ``1000`` 判成不等。
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    left, right = _toNumber(actual), _toNumber(expected)
    if left is not None and right is not None:
        return left == right
    return _asText(actual) == _asText(expected)


def _asText(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value)


def _toNumber(value: Any) -> float | None:
    """尽力转数值；转不了返回 ``None``。布尔**不算**数值（避免 True 比成 1）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------


def dryRun(
    ruleExpression: Mapping[str, Any], examples: Sequence[Any]
) -> DryRunReport:
    """在样例上跑规则，比对实际与期望。

    样例是**审核这条规则的人**给出的期望值（``input`` + ``expectedOutput.matched``），
    形状不对一律 422 —— 把「缺 ``matched``」当成期望 ``False`` 跑出一份绿报告，
    是让审核人误以为规则已验证的最快方式。
    """
    parsed = [_parseExample(raw, index) for index, raw in enumerate(examples)]

    results: list[DryRunCaseResult] = []
    for index, (record, expectedMatched) in enumerate(parsed):
        evaluation = evaluateRule(ruleExpression, record)
        if evaluation.undecidable:
            # 判不了的样例单列一档：混进 FAILED 会把「样例没给全字段」误导成
            # 「规则写错了」，推着审核人去改一条本来正确的规则。
            status = UNDECIDABLE
        elif evaluation.matched == expectedMatched:
            status = PASSED
        else:
            status = FAILED
        results.append(
            DryRunCaseResult(
                index=index,
                input=record,
                expectedMatched=expectedMatched,
                actualMatched=evaluation.matched,
                status=status,
                conditions=evaluation.conditions,
            )
        )
    return DryRunReport(results=tuple(results))


def _parseExample(raw: Any, index: int) -> tuple[dict[str, Any], bool]:
    """校验并归一化一条样例（只取 ``input`` 与 ``expectedOutput.matched``）。"""
    if not isinstance(raw, Mapping):
        raise ValidationError(MSG_WIKI_RULE_EXAMPLE_INVALID.format(index=index))

    record = raw.get("input")
    if not isinstance(record, Mapping):
        raise ValidationError(MSG_WIKI_RULE_EXAMPLE_INVALID.format(index=index))

    expectedOutput = raw.get("expectedOutput")
    if not isinstance(expectedOutput, Mapping):
        raise ValidationError(MSG_WIKI_RULE_EXAMPLE_INVALID.format(index=index))

    matched = expectedOutput.get("matched")
    # 严格 bool：Pydantic 宽松模式会把 "yes" 转成 True，这里的 ``isinstance``
    # 是形状校验的唯一出口，不能被隐式转换绕过。
    if not isinstance(matched, bool):
        raise ValidationError(MSG_WIKI_RULE_EXAMPLE_INVALID.format(index=index))

    return dict(record), matched


__all__ = [
    "PASSED",
    "FAILED",
    "UNDECIDABLE",
    "ConditionResult",
    "RuleEvaluation",
    "DryRunCaseResult",
    "DryRunReport",
    "normalizeOperator",
    "requireOperator",
    "evaluateRule",
    "dryRun",
]
