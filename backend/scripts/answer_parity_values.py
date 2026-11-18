"""A5 比较层的「值」部分：L2 结果集、L4 关键数值、L5 答案等价。

L2 的契约是「**行序无关的多重集**」，不是「列名无关」：两态返回同一列名集合、
同一批值才算等价。列名被忽略会让「数量 1000」与「金额 1000」判成一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_AMOUNT_TOLERANCE = Decimal("0.01")
_RATIO_TOLERANCE = Decimal("0.1")

# 明确的「答不了」标记：出现任一即判结论类别 = INSUFFICIENT（L5 用）。
_INSUFFICIENT_MARKERS = (
    "未接入",
    "无权限",
    "无法回答",
    "无法生成",
    "不支持",
    "无法确定",
    "没有相关数据",
    "无相关数据",
    "数据不足",
    "查不到",
)


# ---------------------------------------------------------------------------
# L2：结果集（行序无关多重集；列名参与比较）
# ---------------------------------------------------------------------------


def _normalizeValue(value: Any) -> Any:
    if isinstance(value, Decimal):
        return round(float(value), 6)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, str):
        return value.strip()
    return value


def _normalizeRows(rows: list[dict[str, Any]]) -> tuple[tuple[tuple[str, Any], ...], ...]:
    """每行 = 排序后的 `(列名, 值)` 对；列顺序不算差异，**列集合算**。"""
    normalized = [
        tuple((key, _normalizeValue(row[key])) for key in sorted(row)) for row in rows
    ]
    return tuple(sorted(normalized, key=repr))


def _valuesEqual(left: Any, right: Any, absTolerance: float) -> bool:
    # 字符串就是字符串：`"0001"` 与 `"1"` 是不同编码，不能被 float() 抹平。
    if isinstance(left, str) or isinstance(right, str):
        return _normalizeValue(left) == _normalizeValue(right)
    try:
        leftNumber = float(left)
        rightNumber = float(right)
    except (TypeError, ValueError):
        return left == right
    return abs(leftNumber - rightNumber) <= absTolerance


def _rowsEqual(
    left: tuple[tuple[str, Any], ...],
    right: tuple[tuple[str, Any], ...],
    absTolerance: float,
) -> bool:
    if [key for key, _value in left] != [key for key, _value in right]:
        return False
    return all(
        _valuesEqual(lv, rv, absTolerance)
        for (_lk, lv), (_rk, rv) in zip(left, right, strict=True)
    )


@dataclass(frozen=True)
class RowComparison:
    equal: bool
    offOnly: int
    onOnly: int
    sample: str


def compareResultSets(
    offRows: list[dict[str, Any]] | None,
    onRows: list[dict[str, Any]] | None,
    *,
    absTolerance: float = 1e-6,
) -> RowComparison | None:
    """L2：**行序/列序无关**的多重集比较；任一侧缺席 ⇒ `None`（该层不可判）。"""
    if offRows is None or onRows is None:
        return None
    remaining = list(_normalizeRows(onRows))
    unmatchedLeft = 0
    sample = ""
    for leftRow in _normalizeRows(offRows):
        matchIndex = next(
            (
                index
                for index, rightRow in enumerate(remaining)
                if _rowsEqual(leftRow, rightRow, absTolerance)
            ),
            None,
        )
        if matchIndex is None:
            unmatchedLeft += 1
            if not sample:
                sample = repr(leftRow)
            continue
        remaining.pop(matchIndex)
    return RowComparison(
        equal=unmatchedLeft == 0 and not remaining,
        offOnly=unmatchedLeft,
        onOnly=len(remaining),
        sample=sample or (repr(remaining[0]) if remaining else ""),
    )


# ---------------------------------------------------------------------------
# L4：答案里的关键数值
# ---------------------------------------------------------------------------

# 数字本体：允许千分位逗号（「1,234.56 元」不能被拆成 234.56）
_NUMBER_BODY = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ratio", re.compile(rf"({_NUMBER_BODY})\s*%")),
    ("amount", re.compile(rf"({_NUMBER_BODY})\s*(亿元|万元|元)")),
    ("count", re.compile(rf"(?<![\w.])({_NUMBER_BODY})(?![\w.%])")),
)
_AMOUNT_SCALE = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}


@dataclass(frozen=True)
class NumberToken:
    kind: str
    value: Decimal

    def __str__(self) -> str:
        return f"{self.kind}:{self.value.normalize()}"


def extractNumbers(text: str) -> tuple[NumberToken, ...]:
    """抽取答案里的关键数值（v0 启发式）。

    已知局限（写进报告，不当成精确解析）：日期会被拆成数字。⇒ **L4 只在有
    「答案文本」的档案回放里启用**，实跑档默认不判。
    """
    spans: list[tuple[int, int]] = []
    tokens: list[NumberToken] = []
    for kind, pattern in _NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < usedEnd and usedStart < end for usedStart, usedEnd in spans):
                continue
            try:
                raw = Decimal(match.group(1).replace(",", ""))
            except InvalidOperation:
                continue
            if kind == "amount":
                raw = raw * _AMOUNT_SCALE.get(match.group(2), Decimal("1"))
            tokens.append(NumberToken(kind=kind, value=raw))
            spans.append((start, end))
    return tuple(tokens)


@dataclass(frozen=True)
class NumberComparison:
    equal: bool
    offOnly: tuple[str, ...]
    onOnly: tuple[str, ...]


def _numberTolerance(kind: str) -> Decimal:
    if kind == "amount":
        return _AMOUNT_TOLERANCE
    if kind == "ratio":
        return _RATIO_TOLERANCE
    return Decimal("0")


def compareNumbers(
    offText: str | None, onText: str | None
) -> NumberComparison | None:
    """L4：数值多重集按容差比；任一侧缺席、或**两侧都没有数值** ⇒ `None`。

    两侧都抽不到数字时「差异 0」是空集通过：文本答案换了供应商名 / 结论措辞
    也会被判一致。判不了就判不了（退出码 6），不许给 0。
    """
    if offText is None or onText is None:
        return None
    offTokens = extractNumbers(offText)
    if not offTokens and not extractNumbers(onText):
        return None
    remaining = list(extractNumbers(onText))
    offOnly: list[str] = []
    for token in offTokens:
        matchIndex = next(
            (
                index
                for index, other in enumerate(remaining)
                if other.kind == token.kind
                and abs(other.value - token.value) <= _numberTolerance(token.kind)
            ),
            None,
        )
        if matchIndex is None:
            offOnly.append(str(token))
            continue
        remaining.pop(matchIndex)
    return NumberComparison(
        equal=not offOnly and not remaining,
        offOnly=tuple(sorted(offOnly)),
        onOnly=tuple(sorted(str(token) for token in remaining)),
    )


# ---------------------------------------------------------------------------
# L5：答案等价（结论类别 + 关键数值）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerElements:
    conclusion: str
    numbers: tuple[NumberToken, ...]


def answerElements(text: str) -> AnswerElements:
    """把答案压成结构化要素；v0 = 结论类别 + 关键数值。"""
    conclusion = (
        "INSUFFICIENT"
        if any(marker in text for marker in _INSUFFICIENT_MARKERS)
        else "ANSWERED"
    )
    return AnswerElements(conclusion=conclusion, numbers=extractNumbers(text))


def answersEquivalent(offText: str | None, onText: str | None) -> bool | None:
    """L5：结论类别相同 **且** 关键数值等价；任一侧缺席 ⇒ `None`。"""
    if offText is None or onText is None:
        return None
    off = answerElements(offText)
    on = answerElements(onText)
    if off.conclusion != on.conclusion:
        return False
    comparison = compareNumbers(offText, onText)
    if comparison is None:
        # 两侧都没有可比的数值 ⇒ v0 的 L5 只压得动「未接入 vs 已作答」，
        # 纯文本差异判不了（退出码 6），不能记成等价。
        return None
    return comparison.equal
