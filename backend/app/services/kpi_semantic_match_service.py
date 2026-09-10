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

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

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
    """
    code: str
    confidence: float
    layer: str = "l1_match"


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
            return KpiMatchResult(code=code, confidence=1.0)
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


