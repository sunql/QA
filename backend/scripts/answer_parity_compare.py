"""A5 比较层入口：退出码契约 + 逐题判定（L1–L5、同态抖动 J）+ 问题集摘要。

分层实现在两个子模块里，这里只做编排：
- `answer_parity_sql`：L1 规范化、L3 口径元素集合；
- `answer_parity_values`：L2 结果集、L4 数值、L5 答案。

判定顺序（M0.4 §4.3/§4.4）：样本完整性 → 同态抖动 J → L3 硬红线 → L2/L4/L5 → L1 定位。
**J > 0 一律判 6**：同态都复现不了时，任何跨态差异都不能归因到开关。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from scripts.answer_parity_sql import (  # noqa: F401 - 对外统一从这里取
    DIALECT,
    CoverageDiff,
    UnparsableSql,
    canonicalSql,
    coverageDiff,
    predicateSet,
)
from scripts.answer_parity_values import (  # noqa: F401 - 对外统一从这里取
    AnswerElements,
    NumberComparison,
    NumberToken,
    RowComparison,
    answerElements,
    answersEquivalent,
    compareNumbers,
    compareResultSets,
    extractNumbers,
)

EXIT_OK = 0
EXIT_NOT_EQUIVALENT = 2
EXIT_ANSWER_MISMATCH = 3
EXIT_PRECONDITION = 4
EXIT_GUARD_BROKEN = 5
EXIT_INCONCLUSIVE = 6

ALL_LAYERS = ("L1", "L2", "L3", "L4", "L5")


@dataclass(frozen=True)
class LayerVerdict:
    layer: str
    status: str  # PASS / DIFF / SKIP / INCONCLUSIVE
    detail: str


@dataclass(frozen=True)
class PairEvaluation:
    """一道题的判定；正常判定只产出 0 / 3 / 6，未知层是使用错误（4）。"""

    exitCode: int
    reason: str
    verdicts: tuple[LayerVerdict, ...]

    @property
    def passed(self) -> bool:
        return self.exitCode == EXIT_OK


def _verdict(layer: str, status: str, detail: str = "") -> LayerVerdict:
    return LayerVerdict(layer=layer, status=status, detail=detail)


def _inconclusive(reason: str, verdicts: list[LayerVerdict]) -> PairEvaluation:
    return PairEvaluation(EXIT_INCONCLUSIVE, reason, tuple(verdicts))


def _preconditionError(
    requiredLayers: tuple[str, ...], sqlSamples: tuple[str | None, ...]
) -> PairEvaluation | None:
    """使用性错误先判：未知层 / 某态没有样本 —— 这两种都不是「答案不一致」。"""
    unknown = [layer for layer in requiredLayers if layer not in ALL_LAYERS]
    if unknown:
        return PairEvaluation(
            EXIT_PRECONDITION, f"未知层：{unknown}", (_verdict("—", "INCONCLUSIVE"),)
        )
    if any(sql is None for sql in sqlSamples):
        return _inconclusive(
            "某态产不出 SQL ⇒ 没有可比对象", [_verdict("L1", "INCONCLUSIVE", "缺少 SQL 样本")]
        )
    return None


def _canonicalSamples(
    off1: str, off2: str, on1: str, on2: str
) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    canonical = {
        "off1": canonicalSql(off1),
        "off2": canonicalSql(off2),
        "on1": canonicalSql(on1),
        "on2": canonicalSql(on2),
    }
    predicates = {
        "off1": predicateSet(off1),
        "off2": predicateSet(off2),
        "on1": predicateSet(on1),
        "on2": predicateSet(on2),
    }
    return canonical, predicates


def _sqlJitter(canonical: dict[str, str], predicates: dict[str, frozenset[str]]) -> list[str]:
    """L1 / L3 的同态抖动。L3 是硬红线 ⇒ 无条件检查（不看 requiredLayers）。"""
    jittered: list[str] = []
    if canonical["off1"] != canonical["off2"] or canonical["on1"] != canonical["on2"]:
        jittered.append("SQL 文本（L1）")
    if predicates["off1"] != predicates["off2"] or predicates["on1"] != predicates["on2"]:
        jittered.append("口径元素（L3）")
    return jittered


def _isEqual(comparison: Any) -> bool | None:
    """比较结果统一取布尔：`RowComparison` / `NumberComparison` **对象恒为真值**，
    直接参与 `and` 会把「值不等」当成真（曾让抖动基线恒判「不抖动」）。"""
    if comparison is None:
        return None
    return bool(getattr(comparison, "equal", comparison))


@dataclass(frozen=True)
class _ValueSamples:
    """某层的两态 × 两遍样本；`None` = 该遍没有这一层的样本。"""

    off1: Any
    off2: Any
    on1: Any
    on2: Any


def _valueLayerJitter(
    layer: str, samples: _ValueSamples
) -> tuple[bool | None, str]:
    """返回 (是否抖动, 说明)。`None` = 第 2 遍样本缺失 ⇒ 抖动不可判。

    **为什么必须要第 2 遍**：L5 的答案是模型合成的文本，SQL 逐字相同也可能
    措辞/数值不同；没有同态第 2 遍就无法排除抖动，任何跨态差异都不能归因。
    """
    if samples.off1 is None or samples.on1 is None:
        return None, "第 1 遍样本缺失"
    if samples.off2 is None or samples.on2 is None:
        return None, "第 2 遍样本缺失 ⇒ 抖动基线 J 无法建立"
    # 同态两遍样本完全相同 ⇒ 直接判不抖动。
    # 否则「两侧都没数字」的答案会因比较器判不了而被记成抖动不可判（假 6）。
    if samples.off1 == samples.off2 and samples.on1 == samples.on2:
        return False, ""
    comparators = {
        "L2": compareResultSets,
        "L4": compareNumbers,
        "L5": answersEquivalent,
    }
    compare = comparators[layer]
    offEqual = _isEqual(compare(samples.off1, samples.off2))
    onEqual = _isEqual(compare(samples.on1, samples.on2))
    if offEqual is None or onEqual is None:
        return None, "样本无法比较"
    # 返回的是「是否抖动」：两个同态都复现 ⇒ 不抖动。
    return not (offEqual and onEqual), ""


def _valueLayerResult(
    layer: str, off: Any, on: Any
) -> tuple[bool | None, str]:
    if layer == "L2":
        comparison = compareResultSets(off, on)
        if comparison is None:
            return None, "结果集未提供"
        return comparison.equal, f"OFF 独有 {comparison.offOnly} 行 / ON 独有 {comparison.onOnly} 行"
    if layer == "L4":
        numbers = compareNumbers(off, on)
        if numbers is None:
            return None, "答案文本未提供"
        return (
            numbers.equal,
            f"数值差异：仅 OFF {list(numbers.offOnly)[:3]} / 仅 ON {list(numbers.onOnly)[:3]}",
        )
    equivalent = answersEquivalent(off, on)
    if equivalent is None:
        return None, "答案文本未提供"
    return equivalent, "结论类别或关键数值不一致"


def _checkValueLayers(
    requiredLayers: tuple[str, ...],
    samplesByLayer: dict[str, _ValueSamples],
) -> tuple[list[LayerVerdict], int | None]:
    """L2/L4/L5：先抖动、再跨态。返回 (verdicts, 早退退出码或 None)。"""
    verdicts: list[LayerVerdict] = []
    for layer in ("L2", "L4", "L5"):
        if layer not in requiredLayers:
            verdicts.append(_verdict(layer, "SKIP", "未要求"))
            continue
        samples = samplesByLayer[layer]
        jitter, jitterDetail = _valueLayerJitter(layer, samples)
        if jitter is None:
            return [
                *verdicts,
                _verdict(layer, "INCONCLUSIVE", jitterDetail),
            ], EXIT_INCONCLUSIVE
        if jitter:
            return [
                *verdicts,
                _verdict(layer, "INCONCLUSIVE", "同态两遍不一致"),
            ], EXIT_INCONCLUSIVE
        equal, detail = _valueLayerResult(layer, samples.off1, samples.on1)
        if equal is None:
            return [*verdicts, _verdict(layer, "INCONCLUSIVE", detail)], EXIT_INCONCLUSIVE
        verdicts.append(_verdict(layer, "PASS" if equal else "DIFF", detail))
    return verdicts, None


def _jitterFailure(jittered: list[str]) -> PairEvaluation:
    l1Jitters = "SQL 文本（L1）" in jittered
    l3Jitters = "口径元素（L3）" in jittered
    return _inconclusive(
        f"同态抖动 J > 0（{' / '.join(jittered)}）⇒ 跨态差异无法归因到开关",
        [
            _verdict("L1", "INCONCLUSIVE" if l1Jitters else "PASS"),
            _verdict(
                "L3",
                "INCONCLUSIVE" if l3Jitters else "PASS",
                "同态口径不一致" if l3Jitters else "",
            ),
        ],
    )


def _coverageFailure(coverage: CoverageDiff, l1Verdict: LayerVerdict) -> PairEvaluation:
    detail = (
        f"口径差异率={coverage.rate:.3f}；"
        f"仅 OFF 有 {list(coverage.offOnly)[:5]}；仅 ON 有 {list(coverage.onOnly)[:5]}"
    )
    return PairEvaluation(
        EXIT_ANSWER_MISMATCH,
        "L3 口径覆盖不一致（硬红线）：" + detail,
        (l1Verdict, _verdict("L3", "DIFF", detail)),
    )


def _valueSamples(
    offRows1: list[dict[str, Any]] | None,
    offRows2: list[dict[str, Any]] | None,
    onRows1: list[dict[str, Any]] | None,
    onRows2: list[dict[str, Any]] | None,
    offAnswer1: str | None,
    offAnswer2: str | None,
    onAnswer1: str | None,
    onAnswer2: str | None,
) -> dict[str, _ValueSamples]:
    return {
        "L2": _ValueSamples(offRows1, offRows2, onRows1, onRows2),
        "L4": _ValueSamples(offAnswer1, offAnswer2, onAnswer1, onAnswer2),
        "L5": _ValueSamples(offAnswer1, offAnswer2, onAnswer1, onAnswer2),
    }


def _finishValueLayers(
    l1Verdict: LayerVerdict,
    coverage: CoverageDiff,
    requiredLayers: tuple[str, ...],
    samplesByLayer: dict[str, _ValueSamples],
) -> PairEvaluation:
    valueVerdicts, earlyExit = _checkValueLayers(requiredLayers, samplesByLayer)
    verdicts = [
        l1Verdict,
        _verdict("L3", "PASS", f"口径元素 {coverage.unionSize} 条，差异 0"),
        *valueVerdicts,
    ]
    if earlyExit is not None:
        reason = next(
            (verdict.detail for verdict in valueVerdicts if verdict.status == "INCONCLUSIVE"),
            "样本不足",
        )
        return _inconclusive(reason, verdicts)
    failed = [verdict.layer for verdict in valueVerdicts if verdict.status == "DIFF"]
    if failed:
        return PairEvaluation(EXIT_ANSWER_MISMATCH, f"跨态答案不一致：{failed}", tuple(verdicts))
    return PairEvaluation(EXIT_OK, "全部要求的层通过", tuple(verdicts))


def evaluateQuestion(
    off1: str | None,
    off2: str | None,
    on1: str | None,
    on2: str | None,
    *,
    requiredLayers: tuple[str, ...] = ("L1", "L3"),
    offRows1: list[dict[str, Any]] | None = None,
    onRows1: list[dict[str, Any]] | None = None,
    offRows2: list[dict[str, Any]] | None = None,
    onRows2: list[dict[str, Any]] | None = None,
    offAnswer1: str | None = None,
    onAnswer1: str | None = None,
    offAnswer2: str | None = None,
    onAnswer2: str | None = None,
) -> PairEvaluation:
    """按 M0.4 §4.3/§4.4 的**自上而下**顺序判定一道题。"""
    preconditionError = _preconditionError(requiredLayers, (off1, off2, on1, on2))
    if preconditionError is not None:
        return preconditionError
    try:
        canonical, predicates = _canonicalSamples(off1, off2, on1, on2)
        coverage = coverageDiff(off1, on1)
    except UnparsableSql as exc:
        return _inconclusive(str(exc), [_verdict("L1", "INCONCLUSIVE", str(exc))])

    jittered = _sqlJitter(canonical, predicates)
    if jittered:
        return _jitterFailure(jittered)

    l1Verdict = _verdict(
        "L1",
        "PASS" if canonical["off1"] == canonical["on1"] else "DIFF",
        "" if canonical["off1"] == canonical["on1"] else "仅文本差异（允许，定位用）",
    )
    if not coverage.isEmpty:
        return _coverageFailure(coverage, l1Verdict)

    samples = _valueSamples(
        offRows1,
        offRows2,
        onRows1,
        onRows2,
        offAnswer1,
        offAnswer2,
        onAnswer1,
        onAnswer2,
    )
    return _finishValueLayers(l1Verdict, coverage, requiredLayers, samples)


def aggregateExitCodes(exitCodes: list[int]) -> int:
    """逐题退出码汇总。

    优先级：2/5/4（评估本身无效：schema 不等价 / 守卫坏 / 前置错误）
    > 3（不通过）> 6（不可判定）> 0。
    在无效前提下判出的「不通过」不可信，所以 2/5/4 压过 3。

    空列表 ⇒ 4：没判过任何题不能算通过（空集假绿是本仓反复踩的坑）。
    """
    if not exitCodes:
        return EXIT_PRECONDITION
    for code in (EXIT_NOT_EQUIVALENT, EXIT_GUARD_BROKEN, EXIT_PRECONDITION):
        if code in exitCodes:
            return code
    if EXIT_ANSWER_MISMATCH in exitCodes:
        return EXIT_ANSWER_MISMATCH
    if any(code != EXIT_OK for code in exitCodes):
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def questionsDigest(payload: dict[str, Any]) -> str:
    """对「除 sha256 之外」的全部内容做规范化摘要（版本区分用，不是签名）。"""
    canonical = {key: value for key, value in payload.items() if key != "sha256"}
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
