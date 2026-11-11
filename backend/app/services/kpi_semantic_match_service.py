"""Phase 1 Task 1.2: KpiSemanticMatchService — L1 语义匹配核心服务。

两层匹配策略：
1. 第一关：精确 alias 匹配 — 用 FEATURE_NAME_RE 提取大写下划线 token，
   命中则直接返回 confidence=1.0（零 LLM 开销）。
2. 第二关：Jaccard 关键词评分 — 提取用户问题中的中文 ngram + 英文单词，
   与 semantic_keywords 数组算 Jaccard 相似度，>= match_threshold 才接受。

返回 KpiMatchResult（frozen dataclass），不修改输入，不依赖 LLM。

缓存抽象（KpiMatchCache）：Task 1.3 替换为 DB-backed 实现。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.domain.models import KpiCatalog

# ---------------------------------------------------------------------------
# Public result DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KpiMatchResult:
    """L1 匹配结果。

    Attributes:
        code: 命中的 KPI code（如 KPI_SUPPLIER_OTD）。
        confidence: 匹配置信度，精确 alias 为 1.0，关键词 Jaccard 为 (0,1)。
        layer: 固定 "l1_match"，供路由指标记录。
        kpi_name: KPI 名称（matchAll 时从缓存填充；match 返回 None）。
        method: 命中路径，``exact_code``（精确 code）/ ``keyword``（Jaccard）/
            ``containment``（v2 包含式）；诊断接口据此解释「为什么命中」，不参与打分。
        matched_phrase: v2 包含式命中时实际命中的短语（诊断可解释性）；其他路径为 None。
    """
    code: str
    confidence: float
    layer: str = "l1_match"
    kpi_name: str | None = None
    method: str = "keyword"
    matched_phrase: str | None = None


# ---------------------------------------------------------------------------
# Public tool function（便于单元测试独立验证）
# ---------------------------------------------------------------------------

def jaccard(a: list[str], b: list[str]) -> float:
    """集合 Jaccard 相似度：|A ∩ B| / |A ∪ B|。

    两集均为空时返回 1.0（未填关键词的指标按完全匹配计），
    其中一集为空时返回 0.0。
    """
    set_a = set(a)
    set_b = set(b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# v2 包含式匹配（设计 §5.1/§5.2）
# ---------------------------------------------------------------------------

# 包含式命中的置信度：规则确定，但与「用户直接点名 code」的身份级命中不同，
# 故低于 1.0。误命中兜底靠最长匹配 + 等长澄清 + 50 条回归集。
CONTAINMENT_CONFIDENCE = 0.9

_CJK_START = "一"
_CJK_END = "鿿"


def _hasCjk(text: str) -> bool:
    return any(_CJK_START <= ch <= _CJK_END for ch in text)


def _cjkCount(text: str) -> int:
    return sum(1 for ch in text if _CJK_START <= ch <= _CJK_END)


def _asciiAlnumCount(text: str) -> int:
    return sum(1 for ch in text if ch.isascii() and ch.isalnum())


def normalizeKpiText(text: str) -> str:
    """匹配归一化：NFKC（全角→半角）→ 小写 → 非字母数字转空格 → 折叠空白。

    标点转**空格**而不是删除：英文短语（"on-time delivery"）与中文分句都需要
    一个分隔符；删除会把 "on-time" 粘成 "ontime"，也会让跨分句的伪短语成立
    （"交付率，不良率" 删掉逗号后可能出现本不存在的长短语）。
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    spaced = "".join(ch if ch.isalnum() else " " for ch in normalized)
    return " ".join(spaced.split())


def isEligiblePhrase(phrase: str) -> bool:
    """命中短语最低长度：CJK ≥ 2 字，或 ASCII 字母数字 ≥ 3（设计 §5.1）。"""
    normalized = normalizeKpiText(phrase)
    return _cjkCount(normalized) >= 2 or _asciiAlnumCount(normalized) >= 3


def phraseMatchesQuestion(phrase: str, normalizedQuestion: str) -> bool:
    """完整包含判定（大小写/全半角已由 normalizeKpiText 处理）。

    - 含 CJK 的短语：子串即命中（中文没有词边界）。
    - 纯 ASCII 短语：左右不得与其他字母/数字相连 —— 否则 "otd" 会命中
      "notdone"，"iqc" 会命中 "biqc" 这类标识符片段。
    """
    normalizedPhrase = normalizeKpiText(phrase)
    if not normalizedPhrase:
        return False
    if _hasCjk(normalizedPhrase):
        return normalizedPhrase in normalizedQuestion
    pattern = rf"(?<![a-z0-9]){re.escape(normalizedPhrase)}(?![a-z0-9])"
    return re.search(pattern, normalizedQuestion) is not None


@dataclass(frozen=True)
class KpiMatchRoute:
    """包含式匹配的路由判定（v2，设计 §5.2）。

    kind 三态：
    - ``hit``：唯一命中最长短语 ⇒ 直接执行，matches 恒为 1 条
    - ``ambiguous``：最长长度并列且分属不同 KPI ⇒ **必须澄清**，不得取 top1
    - ``none``：无命中 ⇒ 回落 LLM 流水线

    matches 恒按 code 升序 —— 歧义集合必须确定性排序，否则同一问题两次
    回答的候选顺序会不同。
    """

    kind: Literal["hit", "ambiguous", "none"]
    matches: tuple[KpiMatchResult, ...] = ()


def _phrasesOf(kpi: KpiCatalog, alias: str | None) -> tuple[str, ...]:
    """一个 KPI 的全部匹配短语源：名称 + 语义关键词 + 挂靠 metric 别名。"""
    phrases: list[str] = [kpi.kpi_name] if kpi.kpi_name else []
    phrases.extend(kpi.semantic_keywords or [])
    if alias:
        phrases.append(alias)
    return tuple(phrases)


def buildKpiClarifyAnswer(question: str, matches: Sequence[KpiMatchResult]) -> str:
    """歧义澄清文案：列出全部候选并请用户确认（不猜、不执行）。"""
    lines = [f"「{question}」可能对应多个指标，请确认您想查询的是哪一个："]
    for index, match in enumerate(matches, start=1):
        lines.append(f"{index}. {match.kpi_name or match.code}（{match.code}）")
    lines.append("请回复指标名称或 KPI 编码，我再按该口径回答。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache abstraction（Task 1.3 替换为 DB-backed）
# ---------------------------------------------------------------------------

class KpiMatchCache:
    """L1 匹配缓存抽象。

    Task 1.3 会替换为启动预热 + 写时失效的 DB-backed 实现。
    当前为纯内存桩，供 unit test 使用。
    """

    def hasCode(self, code: str) -> bool:
        """code 是否存在于缓存中。"""
        raise NotImplementedError

    def findByAnyKeyword(self, keywords: list[str]) -> list["KpiCatalog"]:
        """返回 semantic_keywords 包含任意一个 keyword 的 KPI 列表。

        匹配方式：keyword 是 catalog keyword 的子串（substring），
        英文大小写不敏感。
        """
        raise NotImplementedError

    def is_warmUp(self) -> bool:
        """缓存是否已预热（matchRoute 冷缓存时直接返回 none）。"""
        raise NotImplementedError

    def getAll(self) -> list[KpiCatalog]:
        """全部 PUBLISHED KPI（包含式匹配需要遍历全量短语）。"""
        raise NotImplementedError

    def metricAliasOf(self, code: str) -> str | None:
        """KPI 挂靠 metric 的别名（第三个短语源）；无别名/冷缓存返 None。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Keyword extraction（简单 ngram，生产可用 jieba 替换）
# ---------------------------------------------------------------------------

# 停用词（不计入 ngram）
_STOP_WORDS: frozenset[str] = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
    "请", "帮", "一下", "怎么", "多少", "什么", "哪个", "哪些",
    "吗", "呢", "吧", "啊", "哦", "唉", "呃",
    "情况", "如何", "怎样", "查询", "一下", "给我", "告诉我",
})

# 英文停用词
_ENGLISH_STOP: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "must", "shall",
    "show", "me", "my", "i", "please", "what", "which", "who",
    "how", "where", "when", "then", "than",
})

# 匹配大写下划线标识符（FEATURE_NAME_RE 同款）
_FEATURE_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9_]*_[A-Z0-9_]{1,99})\b")


class KpiSemanticMatchService:
    """L1 语义匹配服务。

    两层策略：
    1. 精确 alias 匹配 — 用户问题含 KPI code 形式大写下划线词 → 1.0 命中。
    2. Jaccard 关键词评分 — 提取中文 ngram + 英文词，与 semantic_keywords
       算 Jaccard，>= match_threshold 时返回最高分者。

    Parameters:
        cache: KpiMatchCache 抽象，Task 1.3 替换为 DB-backed 实现。
    """

    # 复用了 feature_query_service 的正则约定
    FEATURE_NAME_RE = _FEATURE_NAME_RE

    def __init__(self, cache: KpiMatchCache) -> None:
        self._cache = cache

    async def match(self, question: str) -> KpiMatchResult | None:
        """主入口：给定用户问题，返回匹配结果或 None。"""
        if not question or not question.strip():
            return None

        # 第一关：精确 alias
        alias_result = self._matchExactAlias(question)
        if alias_result is not None:
            return alias_result

        # 第二关：关键词 Jaccard
        return self._matchByKeywords(question)

    async def matchAll(
        self, question: str, threshold: float = 0.0, limit: int = 50
    ) -> list[KpiMatchResult]:
        """返回所有命中的 KPI，按 confidence 降序排列。

        Task 1.6 /search 端点专用：支持多候选返回 + 阈值过滤 + limit。
        - 精确 alias 命中 → confidence=1.0
        - 关键词 Jaccard 命中 → confidence >= match_threshold
        """
        if not question or not question.strip():
            return []

        results: list[KpiMatchResult] = []
        seen_codes: set[str] = set()

        # 第一关：精确 alias（cache 未 warmUp 时跳过，避免 RuntimeError）
        try:
            alias_result = self._matchExactAlias(question)
            if alias_result is not None:
                # 从缓存获取 kpi_name
                all_kpis = {kpi.kpi_code: kpi for kpi in self._cache.getAll()}
                alias_kpi = all_kpis.get(alias_result.code)
                alias_result = KpiMatchResult(
                    code=alias_result.code,
                    confidence=alias_result.confidence,
                    layer=alias_result.layer,
                    kpi_name=alias_kpi.kpi_name if alias_kpi else None,
                    method=alias_result.method,
                )
                results.append(alias_result)
                seen_codes.add(alias_result.code)
        except RuntimeError:
            # cache 未 warmUp，跳过精确 alias 分支
            logger.warning("KpiMatchCache not warmUp, skipping exact alias branch")

        # 第二关：关键词 Jaccard（所有候选）
        user_kws = self._extractKeywords(question)
        if user_kws:
            candidates = self._cache.findByAnyKeyword(user_kws)
            for kpi in candidates:
                if kpi.kpi_code in seen_codes:
                    continue
                catalog_kws = kpi.semantic_keywords or []
                if not catalog_kws:
                    continue
                score = jaccard(user_kws, catalog_kws)
                if score >= threshold and score >= float(kpi.match_threshold):
                    results.append(
                        KpiMatchResult(
                            code=kpi.kpi_code,
                            confidence=round(score, 4),
                            kpi_name=kpi.kpi_name,
                        )
                    )
                    seen_codes.add(kpi.kpi_code)

        # 按 confidence 降序
        return sorted(results, key=lambda x: x.confidence, reverse=True)[:limit]

    async def matchRoute(self, question: str) -> KpiMatchRoute:
        """v2 匹配：精确 code → 归一化包含 → 最长短语胜出 → 等长判歧义。

        与 ``match()``/``matchAll()``（Jaccard，保留给诊断与 /search）并存：
        调用方按 ``KPI_MATCH_CONTAINMENT_ENABLED`` 灰度开关选择走哪条。
        """
        if not question or not question.strip():
            return KpiMatchRoute(kind="none")

        exact = self._matchExactAlias(question)
        if exact is not None:
            return KpiMatchRoute(kind="hit", matches=(exact,))

        if not self._cache.is_warmUp():
            return KpiMatchRoute(kind="none")

        normalizedQuestion = normalizeKpiText(question)
        if not normalizedQuestion:
            return KpiMatchRoute(kind="none")

        # (最长短语长度, 命中结果)
        hits: list[tuple[int, KpiMatchResult]] = []
        for kpi in self._cache.getAll():
            bestLength = 0
            bestPhrase = ""
            for phrase in _phrasesOf(kpi, self._cache.metricAliasOf(kpi.kpi_code)):
                if not isEligiblePhrase(phrase):
                    continue
                if not phraseMatchesQuestion(phrase, normalizedQuestion):
                    continue
                length = len(normalizeKpiText(phrase))
                if length > bestLength:
                    bestLength = length
                    bestPhrase = phrase
            if bestLength == 0:
                continue
            hits.append(
                (
                    bestLength,
                    KpiMatchResult(
                        code=kpi.kpi_code,
                        confidence=CONTAINMENT_CONFIDENCE,
                        kpi_name=kpi.kpi_name,
                        method="containment",
                        matched_phrase=bestPhrase,
                    ),
                )
            )

        if not hits:
            return KpiMatchRoute(kind="none")

        hits.sort(key=lambda item: (-item[0], item[1].code))
        topLength = hits[0][0]
        top = tuple(match for length, match in hits if length == topLength)
        if len(top) == 1:
            return KpiMatchRoute(kind="hit", matches=top)
        # 等长跨 KPI 并列：不猜，交给澄清。hits 已按 code 升序。
        return KpiMatchRoute(kind="ambiguous", matches=top)

    # ------------------------------------------------------------------
    # Exact alias match
    # ------------------------------------------------------------------

    def _matchExactAlias(self, question: str) -> KpiMatchResult | None:
        """用 FEATURE_NAME_RE 提取大写下划线 token，命中即返回。"""
        m = self.FEATURE_NAME_RE.search(question)
        if not m:
            return None
        code = m.group(1)
        if self._cache.hasCode(code):
            return KpiMatchResult(code=code, confidence=1.0, method="exact_code")
        return None

    # ------------------------------------------------------------------
    # Keyword Jaccard match
    # ------------------------------------------------------------------

    def _matchByKeywords(self, question: str) -> KpiMatchResult | None:
        """按 brief 算法：提取关键词 → findByAnyKeyword 候选 → Jaccard 评分。"""
        user_kws = self._extractKeywords(question)
        if not user_kws:
            return None

        candidates = self._cache.findByAnyKeyword(user_kws)
        if not candidates:
            return None

        scored: list[tuple["KpiCatalog", float]] = []
        for kpi in candidates:
            catalog_kws = kpi.semantic_keywords or []
            if not catalog_kws:
                continue
            score = jaccard(user_kws, catalog_kws)
            scored.append((kpi, score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[1], reverse=True)
        top_kpi, top_score = scored[0]
        if top_score >= float(top_kpi.match_threshold):
            return KpiMatchResult(code=top_kpi.kpi_code, confidence=round(top_score, 4))
        return None

    @staticmethod
    def _extractKeywords(question: str) -> list[str]:
        """从用户问题中提取中文 ngram（2-4字）+ 英文单词。

        简化实现（生产可用 jieba 替换）：
        - 英文单词：连续字母/数字序列（忽略停用词）
        - 中文 ngram：2/3/4 字滑动窗口（过滤纯停用词窗口）
        """
        keywords: list[str] = []

        # 英文单词
        for tok in question.split():
            clean = re.sub(r"[^a-zA-Z0-9]", "", tok).lower()
            if clean and clean not in _ENGLISH_STOP and len(clean) >= 2:
                keywords.append(clean)

        # 中文 ngram：2/3/4 字
        chinese_chars = re.sub(r"[a-zA-Z0-9\s]", "", question)
        for size in (2, 3, 4):
            for i in range(len(chinese_chars) - size + 1):
                gram = chinese_chars[i : i + size]
                if gram not in _STOP_WORDS:
                    keywords.append(gram)

        return keywords


# ---------------------------------------------------------------------------
# Factory（供 FastAPI Depends 使用）
# ---------------------------------------------------------------------------


def getKpiSemanticMatchService() -> KpiSemanticMatchService:
    """返回绑定了全局 kpi_match_cache 单例的 service 实例。"""
    from app.services.kpi_match_cache import kpi_match_cache

    return KpiSemanticMatchService(kpi_match_cache)

