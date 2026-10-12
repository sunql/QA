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

# I1：否定式方向词作废（位置性判定，**闭集 + 小句内向后窗口**）。全文扫描会误伤
# 「无锡」「未来」「无法」这类词里的否定字，故从方向词**向后**只看窗口内、且不跨小句。
NEGATION_PARTICLES: frozenset[str] = frozenset("不未没无非别莫")

# I1：向后扫描窗口上界（字符数）。方向词之前的否定字**至多**隔 W 个字符才算否定：
# 「不应大幅下降」（«不» 距 «下降» = 4）命中；「无法确认库存增长」（«无» 距 «增长» = 6）
# 落在窗外 ⇒ 不误伤。
NEGATION_SCAN_WINDOW = 5

# I1：窗口内一旦命中这些转折/连接词，就**否掉**前面否定式的语义（扫到此停 ⇒ 不算否定）：
# 「不**但**上涨」的 «但»、「不就**是**上升」的 «是/就»。
CLAUSE_STOP_CHARS: frozenset[str] = frozenset("但而且还又也却则是就")

DIRECTION_INCREASE = "increase"
DIRECTION_DECREASE = "decrease"

# I1 limitations 文案：**不复用**「断言未含可判方向」——那句会误导（此处有方向词，只是被
# 否定式作废了）。
NEGATED_DIRECTION_LIMITATION = "断言含否定，方向不可判"


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

    I1 薄壳：否定式方向词先作废（见 :func:`_resolveDirection`）。
    """
    return _resolveDirection(statement)[0]


def _isCjk(char: str) -> bool:
    """是否 CJK 统一表意文字（``一``..``鿿`` = U+4E00..U+9FFF）。

    窗口扫描把**非**此类字符当作小句边界：标点 / 空格 / ASCII 一律断句（故半角与全角标点
    都算边界）。
    """
    return "一" <= char <= "鿿"


def _isNegatedDirectionWordAt(text: str, start: int) -> bool:
    """I1：方向词起始下标 ``start`` 之前、**小句窗口内**是否有否定式（闭集 + 位置）。

    从 ``start - 1`` **向后**逐字符扫描，下界 ``start - NEGATION_SCAN_WINDOW``（只看最多 5
    个字符）。逐字符：

    - 命中否定助词（``不未没无非别莫``）⇒ ``True``（判否定）；
    - 命中转折/连接词（:data:`CLAUSE_STOP_CHARS`）⇒ ``False``（它否掉前面的否定式语义，
      如「不**但**上涨」「不就**是**上升」）；
    - 命中非汉字符（标点/空格/ASCII ⇒ 小句边界）⇒ ``False``；
    - 否则继续向后。扫到上界仍未命中 ⇒ ``False``。

    窗口取 ``W=5``：`不应大幅下降`（«不» 距 «下降» = 4）命中；`无法确认库存增长`
    （«无» 距 «增长» = 6）落在窗外 ⇒ 不误伤。

    **已知残余（不是完备判据，别再假装完备）**——裸列名 + 取值下拿不到语法树，以下四类
    仍会判错，是否收窄交由控制器裁定（**别**在此自行扩张判据）：

    1. **窗口外漏判（符号翻转，危害最大）**：``W=5`` 之外的修饰仍看不见 —— 如
       「不应该出现大幅下降」（«不» 距 «下降» = 7）、「不应出现明显下降」（= 6）仍会被判成
       方向词有效。
    2. **距离按字符数、非词边界 ⇒ 误伤（吞掉）**：**非否定**词里的否定字，只要落在窗口内就
       会被当成否定，方向被丢成 ``None``。现实输入里最常见的是这两个子类：
       - 以否定字开头的常用词：``非常`` / ``特别`` / ``不仅`` / ``未来`` / ``无法`` / ``无锡``
         / ``不锈钢``（**本域真实商品名**）—— 如「库存非常明显地增长」（«非» 距 «增长» = 5）、
         「不锈钢库存增长」（«不» 距 «增长» = 5）、「不仅增长了销量」「未来将持续下降」。
       - 单字否定字嵌在词中：如「特别」的 «别»。
       这是相对上一轮「紧邻前缀」判据的**行为回归**：旧实现（前缀 ≤ 2 字）能躲开，新窗口反
       而够得到。既有 FP 行「未来三月采购额上升」只是碰巧距离 7 > W 才没翻红，**不是**词边界
       在挡 —— 故此矩阵**不能**证明该子类安全（见测试同名矩阵）。
    3. **无标点的多小句**：如「无问题销量下降」「问题不大销售下降」没有小句边界，会把前一小
       句的否定式误带到方向词上（``None``）。
    4. **非汉字符截断扫描 ⇒ 同小句内漏判**：否定字与方向词之间夹了 ASCII / 数字 / 空格 / 标点
       时扫描提前停（判成小句边界）—— 如「不应 下降」（«不» 距 «下降» = 3 ≤ W 却漏判）、
       「不达5%下降」（«%» 截断）。
    """
    lowerBound = max(0, start - NEGATION_SCAN_WINDOW)
    for index in range(start - 1, lowerBound - 1, -1):
        char = text[index]
        if char in NEGATION_PARTICLES:
            return True
        if char in CLAUSE_STOP_CHARS:
            return False
        if not _isCjk(char):
            return False
    return False


def _scanDirectionWords(text: str, words: tuple[str, ...]) -> tuple[int, bool]:
    """统计 ``words`` 的出现：返回 ``(未被否定的次数, 是否出现过被否定的出现)``。"""
    active = 0
    negated = False
    for word in words:
        start = text.find(word)
        while start != -1:
            if _isNegatedDirectionWordAt(text, start):
                negated = True
            else:
                active += 1
            start = text.find(word, start + 1)
    return active, negated


def _resolveDirection(statement: str | None) -> tuple[str | None, bool]:
    """返回 ``(方向, 是否出现被否定的方向词)``。

    否定式方向词**作废**；作废后仍无有效方向词（或涨跌两向同时有效 ⇒ 互相矛盾）⇒ ``None``。
    """
    text = statement or ""
    increaseActive, increaseNegated = _scanDirectionWords(text, INCREASE_DIRECTION_WORDS)
    decreaseActive, decreaseNegated = _scanDirectionWords(text, DECREASE_DIRECTION_WORDS)
    negated = increaseNegated or decreaseNegated
    if increaseActive and not decreaseActive:
        return DIRECTION_INCREASE, negated
    if decreaseActive and not increaseActive:
        return DIRECTION_DECREASE, negated
    return None, negated


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
    direction, negatedDirection = _resolveDirection(assertion)
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
        # I1：区分「没有方向词」与「有方向词但被否定式作废」——后者绝不能复用前者文案。
        if negatedDirection:
            limitations.append(NEGATED_DIRECTION_LIMITATION)
        else:
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
