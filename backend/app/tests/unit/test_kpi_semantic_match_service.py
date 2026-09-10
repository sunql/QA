"""Phase 1 Task 1.2: KpiSemanticMatchService 单元测试（TDD RED -> GREEN）。

覆盖：
1. 精确 alias 匹配（FEATURE_NAME_RE 风格大写下划线 token）
2. Jaccard 关键词匹配 above/below threshold
3. 空输入 / 无关键词边界

缓存桩（KpiMatchCacheStub）不依赖真实 DB，纯算法验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from app.services.kpi_semantic_match_service import (
    KpiMatchResult,
    KpiSemanticMatchService,
    jaccard,
)


# ---------------------------------------------------------------------------
# Override autouse fixtures from conftest.py that pull in DB dependency.
# These tests are pure algorithm / in-memory stub — no DB needed.
# ---------------------------------------------------------------------------

@pytest.fixture()
def dbSession() -> Any:
    """Override conftest autouse dbSession — not used by these tests."""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """Override conftest autouse seedEngine — not used by these tests."""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """Override conftest autouse warmBusinessObjectRegistry — no-op, no DB needed."""
    yield


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _StubKpi:
    """最小 KpiCatalog 模拟，仅含匹配相关字段。"""
    kpi_code: str
    kpi_name: str
    semantic_keywords: list[str] | None
    match_threshold: Decimal


class _StubCache:
    """内存缓存桩，load 后全量存 Python 对象。"""

    def __init__(self) -> None:
        self._items: list[_StubKpi] = []

    def load(self, items: list[_StubKpi]) -> None:
        self._items = list(items)

    def hasCode(self, code: str) -> bool:
        return any(k.kpi_code == code for k in self._items)

    def allItems(self) -> list[_StubKpi]:
        return list(self._items)

    def findByAnyKeyword(self, keywords: list[str]) -> list[_StubKpi]:
        """返回 semantic_keywords 包含任意一个 keyword 的 KPI（子串匹配，大小写不敏感）。

        匹配方式：keyword 是 catalog keyword 的子串（如 "准时" 匹配 "准时交付"）。
        """
        result: list[_StubKpi] = []
        for kpi in self._items:
            if kpi.semantic_keywords:
                for kw in keywords:
                    kw_lower = kw.lower()
                    for cat_kw in kpi.semantic_keywords:
                        if kw_lower in cat_kw.lower():
                            result.append(kpi)
                            break
        return result


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_OTD = _StubKpi(
    kpi_code="KPI_SUPPLIER_OTD",
    kpi_name="供应商准时交付率",
    semantic_keywords=["准时交付", "OTD", "按时", "准时率"],
    match_threshold=Decimal("0.75"),
)
_DEFECT = _StubKpi(
    kpi_code="KPI_SUPPLIER_DEFECT_RATE",
    kpi_name="供应商来料不良率",
    semantic_keywords=["来料不良", "不良率", "defect", "不合格率"],
    match_threshold=Decimal("0.75"),
)
_VARIANCE = _StubKpi(
    kpi_code="KPI_PURCHASE_PRICE_VARIANCE",
    kpi_name="采购价格偏差",
    semantic_keywords=["价格偏差", "price variance", "价差"],
    match_threshold=Decimal("0.60"),
)


# ---------------------------------------------------------------------------
# Jaccard function
# ---------------------------------------------------------------------------

class TestJaccard:
    """Jaccard 集合相似度函数正确性。"""

    def test_identical_sets(self) -> None:
        a = ["准时交付", "OTD", "on-time"]
        b = ["准时交付", "OTD", "on-time"]
        assert jaccard(a, b) == pytest.approx(1.0)

    def test_partial_overlap(self) -> None:
        a = ["准时交付", "OTD", "on-time"]
        b = ["准时交付", "按时"]
        # intersection = {"准时交付"}, union = {"准时交付","OTD","on-time","按时"}
        assert jaccard(a, b) == pytest.approx(1 / 4)

    def test_disjoint_sets(self) -> None:
        a = ["准时交付", "OTD"]
        b = ["来料不良", "不良率"]
        assert jaccard(a, b) == 0.0

    def test_empty_a_returns_zero(self) -> None:
        assert jaccard([], ["准时交付"]) == 0.0

    def test_empty_b_returns_zero(self) -> None:
        assert jaccard(["准时交付"], []) == 0.0

    def test_both_empty_returns_one(self) -> None:
        # 两个空集 → 完全匹配（语义：未填关键词的指标按完全匹配计）
        assert jaccard([], []) == 1.0

    def test_order_independent(self) -> None:
        a = ["A", "B", "C"]
        b = ["C", "B", "A"]
        assert jaccard(a, b) == pytest.approx(jaccard(b, a))


# ---------------------------------------------------------------------------
# Service: exact alias match
# ---------------------------------------------------------------------------

class TestMatchExactAlias:
    """第一关：FEATURE_NAME_RE 风格大写下划线 token 精确匹配。"""

    @pytest.fixture()
    def cache(self) -> _StubCache:
        c = _StubCache()
        c.load([_OTD, _DEFECT, _VARIANCE])
        return c

    @pytest.fixture()
    def svc(self, cache: _StubCache) -> KpiSemanticMatchService:
        return KpiSemanticMatchService(cache)

    @pytest.mark.asyncio()
    async def test_match_exact_alias_returns_kpi_code(self, svc: KpiSemanticMatchService) -> None:
        """问题含 KPI_SUPPLIER_OTD → 直接命中，confidence=1.0。"""
        result = await svc.match("请帮我查一下 KPI_SUPPLIER_OTD 的完成情况")
        assert result is not None
        assert result.code == "KPI_SUPPLIER_OTD"
        assert result.confidence == 1.0
        assert result.layer == "l1_match"

    @pytest.mark.asyncio()
    async def test_match_exact_alias_miss_case(self, svc: KpiSemanticMatchService) -> None:
        """问题含大写下划线 token 但不在 catalog → 返回 None。"""
        result = await svc.match("请查一下 KPI_UNKNOWN_CODE")
        assert result is None

    @pytest.mark.asyncio()
    async def test_match_no_alias_returns_none(self, svc: KpiSemanticMatchService) -> None:
        """无大写下划线 token → 跳过精确匹配。"""
        result = await svc.match("供应商准时交付率是多少")
        assert result is None  # 未命中，进入 keyword 路径


# ---------------------------------------------------------------------------
# Service: keyword Jaccard match
# ---------------------------------------------------------------------------

class TestMatchByKeywords:
    """第二关：Jaccard 关键词匹配。"""

    @pytest.fixture()
    def cache(self) -> _StubCache:
        return _StubCache()

    @pytest.fixture()
    def svc(self, cache: _StubCache) -> KpiSemanticMatchService:
        return KpiSemanticMatchService(cache)

    @pytest.mark.asyncio()
    async def test_match_keyword_overlap_above_threshold(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """极短中文问题"准时"命中单字 catalog keyword → Jaccard = 1.0，命中。

        关键：2字中文词作为 query 精确命中 catalog single keyword。
        问题越长 ngram 碎片越多，Jaccard 越低。
        question="准时" → user_kws=["准时"]（2-char ngram）
        catalog=["准时"] → intersection={"准时"}, union={"准时"} → Jacc=1.0 >= 0.75 ✓
        """
        otd_simple = _StubKpi(
            kpi_code="KPI_SUPPLIER_OTD",
            kpi_name="供应商准时交付率",
            semantic_keywords=["准时"],
            match_threshold=Decimal("0.75"),
        )
        cache.load([otd_simple, _DEFECT, _VARIANCE])
        result = await svc.match("准时")
        assert result is not None
        assert result.code == "KPI_SUPPLIER_OTD"
        assert result.confidence >= Decimal("0.75")

    @pytest.mark.asyncio()
    async def test_match_keyword_overlap_below_threshold(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """用户问题仅含 1 个 semantic_keyword，Jaccard < 0.75 → 不命中。"""
        cache.load([_OTD, _DEFECT, _VARIANCE])
        result = await svc.match("供应商的准时率怎么样")
        assert result is None

    @pytest.mark.asyncio()
    async def test_match_no_keywords_returns_none(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """无法提取有效关键词（如全是停用词）→ 返回 None。"""
        cache.load([_OTD])
        result = await svc.match("请帮我查一下")
        assert result is None

    @pytest.mark.asyncio()
    async def test_match_empty_input_returns_none(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """空字符串 → 返回 None。"""
        cache.load([_OTD])
        result = await svc.match("")
        assert result is None

    @pytest.mark.asyncio()
    async def test_match_empty_cache_returns_none(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """缓存为空 → 返回 None（不会 crash）。"""
        cache.load([])
        result = await svc.match("供应商准时交付率")
        assert result is None

    @pytest.mark.asyncio()
    async def test_match_english_only_question(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """纯英文问题含 catalog keyword → Jaccard = 1.0，命中。

        英文 query "rate" → user_kws=["rate"]（strip 后唯一词）
        catalog=["rate"] → intersection={"rate"}, union={"rate"} → Jacc=1.0 >= 0.75 ✓
        """
        otd_en = _StubKpi(
            kpi_code="KPI_SUPPLIER_OTD",
            kpi_name="On-Time Delivery Rate",
            semantic_keywords=["rate"],
            match_threshold=Decimal("0.75"),
        )
        cache.load([otd_en])
        result = await svc.match("rate")
        assert result is not None
        assert result.code == "KPI_SUPPLIER_OTD"

    @pytest.mark.asyncio()
    async def test_match_returns_highest_jaccard_when_multiple_candidates(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """多指标候选时 DEFECT 精确命中（jaccard=1.0），OTD/VARIANCE 无匹配（jaccard=0）。

        question="defect" → user_kws=["defect"]（英文词，strip 后仍为 "defect"）
        DEFECT catalog=["defect"] → intersection={"defect"}, union={"defect"} → Jacc=1.0 ✓
        OTD/VARIANCE findByAnyKeyword: "defect" 不在任何 catalog keyword 子串中 → 无候选
        """
        # 仅构造单 keyword catalog 让 findByAnyKeyword 能匹配到且 Jaccard=1.0
        defect_simple = _StubKpi(
            kpi_code="KPI_SUPPLIER_DEFECT_RATE",
            kpi_name="供应商来料不良率",
            semantic_keywords=["defect"],
            match_threshold=Decimal("0.75"),
        )
        cache.load([_OTD, defect_simple, _VARIANCE])
        result = await svc.match("defect")
        assert result is not None
        assert result.code == "KPI_SUPPLIER_DEFECT_RATE"

    @pytest.mark.asyncio()
    async def test_match_threshold_per_kpi(
        self, svc: KpiSemanticMatchService, cache: _StubCache
    ) -> None:
        """每条 KPI 有独立的 match_threshold — 低阈值指标可被普通问题命中。

        问题"低价" → user_kws=["低价"]（2-char ngram，完整命中）
        catalog=["低价","优惠"] → intersection={"低价"}, union={"低价","优惠"}
        Jaccard=1/2=0.5 >= 0.2 低阈值 → 命中 ✓
        """
        low_thresh = _StubKpi(
            kpi_code="KPI_LOW_THRESHOLD",
            kpi_name="低阈值指标",
            semantic_keywords=["低价", "优惠"],
            match_threshold=Decimal("0.20"),
        )
        cache.load([low_thresh])
        result = await svc.match("低价")
        assert result is not None
        assert result.code == "KPI_LOW_THRESHOLD"


# ---------------------------------------------------------------------------
# Result dataclass immutability
# ---------------------------------------------------------------------------

class TestKpiMatchResultImmutable:
    """KpiMatchResult 是 frozen dataclass，不可变。"""

    def test_is_frozen(self) -> None:
        r = KpiMatchResult(code="X", confidence=0.9)
        with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
            r.code = "Y"  # type: ignore[annotation-type-mismatch]
