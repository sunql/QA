"""假设验证的**数据支持度**判定（Task 5）：把一条候选假设 + 证据摘要判成
``supported`` / ``contradicted`` / ``inconclusive`` / ``execution_failed``。

设计口径（controller D16–D22，优先于 plan 原文）：

**D20b · 候选只有三个键。** ``Hypothesis`` 是 frozen dataclass，字段恰好
``statement`` / ``driver`` / ``verificationSql``（``hypothesis_service.py:91-98``）—— plan
原文读的 ``assertion`` / ``metric`` / ``direction`` / ``threshold`` **四个键全不存在**。
照抄的结果是 ``direction`` 恒 ``None`` ⇒ 两个分支永不命中 ⇒ **verdict 恒 INCONCLUSIVE**
（一个「跑得通、永远给不出结论」的静默失效）。故：

- ``assertion`` 取 ``statement``；
- ``metric`` 取 ``driver``（它就是「列名 / 指标名」，见 ``Hypothesis`` 注释）；
- ``direction`` **必须从 statement 的中文方向词推导**（:func:`deriveDirection`）；两向都
  拿不到（或互相矛盾）⇒ ``None`` ⇒ ``INCONCLUSIVE``（宁可不判，不许瞎猜）；
- ``threshold`` 是 :func:`evaluateHypothesis` 的**显式入参**（默认 ``None``），由
  :attr:`HypothesisTest.threshold` 承载 —— **不是**候选的键。

**D19b · 摘要的数值字段是字符串。** ``EvidenceSummary.baseline`` / ``current`` 是
``str | None``（``research_evidence_summary``，与 ``research_json`` 的 ``Decimal → str``
口径一致、无损）。比较前**必须**经 :meth:`EvidenceSummary.numeric` 归一为 ``Decimal`` ——
裸串比较是**字典序**（``"1000" > "900"`` 为 ``False``），会**静默把大小判反**（涨了判成
跌了，且不报错）；直接拿 ``Decimal`` 比字符串则当场 ``TypeError``。

**D20 · 无阈值会过度断言。** 只要方向相反就判 ``CONTRADICTED``（跌 0.001% 与跌 90% 同罪），
``SUPPORTED`` 同理。故：``threshold`` 有值 ⇒ ``|current - baseline| < threshold`` 判
``INCONCLUSIVE``；无值 ⇒ **只在反向变化**时判 ``CONTRADICTED``，**恰好相等** ⇒
``INCONCLUSIVE``（数据既没支持也没反驳）。

**D18 · ``verdict`` 与 ``execution_succeeded`` 正交。** 前者是「数据支持度」，后者是「验证
SQL 是否跑通」；``execution_error`` 非空 ⇒ ``EXECUTION_FAILED``（不是 ``CONTRADICTED``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from app.services.research_evidence_summary import EvidenceSummary

# 中文方向词（D20b）：从 statement 推导断言方向。涨词与跌词**互斥**；两向都命中或都不命中
# ⇒ 方向不可定 ⇒ None（宁可不判，不许瞎猜）。
INCREASE_DIRECTION_WORDS: tuple[str, ...] = ("涨", "上升", "增长", "增加", "提高")
DECREASE_DIRECTION_WORDS: tuple[str, ...] = ("跌", "下降", "减少", "降低", "下滑")

DIRECTION_INCREASE = "increase"
DIRECTION_DECREASE = "decrease"


class HypothesisVerdict(str, Enum):
    """假设的**数据支持度**（D18：与「执行是否成功」正交）。"""

    SUPPORTED = "supported"  # 数据支持该断言（且执行成功）
    CONTRADICTED = "contradicted"  # 数据与该断言方向相反
    INCONCLUSIVE = "inconclusive"  # 证据不足 / 方向不可判 / 变化不显著
    EXECUTION_FAILED = "execution_failed"  # 验证 SQL 未跑通


@dataclass(frozen=True)
class HypothesisTest:
    """一条假设的验证结果（不可变）。"""

    assertion: str
    metric: str
    direction: str | None
    threshold: Decimal | None
    baseline: Decimal | None
    current: Decimal | None
    verdict: HypothesisVerdict
    evidence_summary: dict[str, Any]
    execution_succeeded: bool
    sample_size: int
    limitations: list[str]


def deriveDirection(statement: str | None) -> str | None:
    """纯函数（D20b）：从 statement 的中文方向词推导 ``increase`` / ``decrease``。

    两向都命中（互相矛盾）或都不命中 ⇒ ``None``（方向不可定，绝不瞎猜）。
    """
    text = statement or ""
    hasIncrease = any(word in text for word in INCREASE_DIRECTION_WORDS)
    hasDecrease = any(word in text for word in DECREASE_DIRECTION_WORDS)
    if hasIncrease and not hasDecrease:
        return DIRECTION_INCREASE
    if hasDecrease and not hasIncrease:
        return DIRECTION_DECREASE
    return None


def evaluateHypothesis(
    candidate: dict[str, Any],
    summary: EvidenceSummary,
    rows: list[dict[str, Any]],
    *,
    threshold: Decimal | None = None,
    execution_error: str | None = None,
) -> HypothesisTest:
    """判定一条候选假设的数据支持度（D16 / D18 / D19b / D20 / D20b）。

    ``candidate`` 是**真实**候选形状（``statement`` / ``driver`` / ``verificationSql``）；
    ``rows`` 是验证 SQL 的原始结果行（用于 ``sample_size``）；``threshold`` 是最小显著变化量
    （显式入参，非候选键）；``execution_error`` 非空 ⇒ 直接 ``EXECUTION_FAILED``。
    """
    assertion = str(candidate.get("statement") or "")
    metric = str(candidate.get("driver") or "")
    direction = deriveDirection(assertion)
    sampleSize = len(rows)

    if execution_error is not None:
        return HypothesisTest(
            assertion=assertion,
            metric=metric,
            direction=direction,
            threshold=threshold,
            baseline=None,
            current=None,
            verdict=HypothesisVerdict.EXECUTION_FAILED,
            evidence_summary=_evidenceDict(summary),
            execution_succeeded=False,
            sample_size=sampleSize,
            limitations=[f"执行失败: {execution_error}"],
        )

    # D19b：字段是字符串，必须经 numeric() 归一为 Decimal 再比较。
    baseline = summary.numeric("baseline")
    current = summary.numeric("current")

    limitations: list[str] = []
    if sampleSize == 0:
        limitations.append("无数据样本")
    if baseline is None:
        limitations.append("缺少基线")
    if direction is None:
        limitations.append("断言未含可判方向")

    verdict = _decideVerdict(
        direction=direction,
        baseline=baseline,
        current=current,
        threshold=threshold,
        sampleSize=sampleSize,
    )
    return HypothesisTest(
        assertion=assertion,
        metric=metric,
        direction=direction,
        threshold=threshold,
        baseline=baseline,
        current=current,
        verdict=verdict,
        evidence_summary=_evidenceDict(summary),
        execution_succeeded=True,
        sample_size=sampleSize,
        limitations=limitations,
    )


def _decideVerdict(
    *,
    direction: str | None,
    baseline: Decimal | None,
    current: Decimal | None,
    threshold: Decimal | None,
    sampleSize: int,
) -> HypothesisVerdict:
    """D20 判据：缺样本 / 缺基线 / 方向不可定 ⇒ INCONCLUSIVE；否则按方向 + 阈值判。"""
    if sampleSize == 0 or baseline is None or current is None or direction is None:
        return HypothesisVerdict.INCONCLUSIVE

    change = current - baseline
    if change == 0:
        # 恰相等：数据既没支持也没反驳（D20 明列）。
        return HypothesisVerdict.INCONCLUSIVE
    if threshold is not None and abs(change) < threshold:
        # 变化量不足最小显著量 ⇒ 不下结论（避免过度断言）。
        return HypothesisVerdict.INCONCLUSIVE

    rising = change > 0
    supports = rising if direction == DIRECTION_INCREASE else not rising
    return HypothesisVerdict.SUPPORTED if supports else HypothesisVerdict.CONTRADICTED


def _evidenceDict(summary: EvidenceSummary | None) -> dict[str, Any]:
    """D19：``mode="json"`` + ``by_alias`` —— ``Decimal`` → **字符串**、``datetime`` → ISO。

    全程 JSON 原生 ⇒ 落库边界 ``_coerceJsonSafe`` 无事可做（no-op），无损且与 Task 3/4 的
    ``Decimal → str`` 口径一致。

    绝不能是 ``summary.__dict__``：那是 snake_case + 驱动原始值（``Decimal`` / ``date``），
    落库时会被 ``_coerceJsonSafe`` 把 ``Decimal`` 变**有损 float**（D19 / D22）。也绝不能把
    模型对象本身塞进去 —— 兜底分支会把它整块渲染成一个不透明字符串（静默损坏、不报错）。
    """
    if summary is None:
        return {}
    return summary.model_dump(mode="json", by_alias=True)
