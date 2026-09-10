"""Phase 1 Task 1.6: KPI catalog /search 端点单元测试。

覆盖：
- matchAll 返回多候选（exact alias + keyword Jaccard 重复去重）
- matchAll threshold 过滤
- matchAll limit 截断
- 空输入返回空列表

纯算法验证，不依赖 DB（复用 test_kpi_semantic_match_service.py 的 _StubCache）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
import pytest_asyncio

from app.services.kpi_semantic_match_service import (
    KpiMatchResult,
    KpiSemanticMatchService,
)


# ---------------------------------------------------------------------------
# Override autouse fixtures from conftest.py that pull in DB dependency.
# ---------------------------------------------------------------------------

@pytest.fixture()
def dbSession():
    """Override conftest autouse dbSession — not used by these tests."""
    return None


@pytest_asyncio.fixture()
async def seedEngine():
    """Override conftest autouse seedEngine — not used by these tests."""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry():
    """Override conftest autouse warmBusinessObjectRegistry — no-op, no DB needed."""
    yield


# ---------------------------------------------------------------------------
# Stub KPI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubKpi:
    kpi_code: str
    kpi_name: str
    semantic_keywords: list[str] | None
    match_threshold: Decimal


class _StubCache:
    """内存缓存桩（兼容 KpiMatchCache 接口）。"""

    def __init__(self) -> None:
        self._items: list[_StubKpi] = []

    def load(self, items: list[_StubKpi]) -> None:
        self._items = list(items)

    def hasCode(self, code: str) -> bool:
        return any(k.kpi_code == code for k in self._items)

    def getAll(self) -> list[_StubKpi]:
        return list(self._items)

    def findByAnyKeyword(self, keywords: list[str]) -> list[_StubKpi]:
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
# Tests: matchAll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestMatchAll:
    """KpiSemanticMatchService.matchAll 行为验证。"""

    @pytest.fixture()
    def cache(self) -> _StubCache:
        c = _StubCache()
        c.load([_OTD, _DEFECT, _VARIANCE])
        return c

    @pytest.fixture()
    def svc(self, cache: _StubCache) -> KpiSemanticMatchService:
        return KpiSemanticMatchService(cache)

    async def test_matchAll_returns_high_confidence_first(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """多候选按 confidence 降序排列。"""
        results = await svc.matchAll("准时", threshold=0.0, limit=10)
        if results:
            # confidence 降序
            for i in range(len(results) - 1):
                assert results[i].confidence >= results[i + 1].confidence

    async def test_matchAll_empty_input_returns_empty(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """空字符串 → 返回空列表。"""
        results = await svc.matchAll("", threshold=0.0, limit=10)
        assert results == []

    async def test_matchAll_whitespace_input_returns_empty(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """纯空白字符串 → 返回空列表。"""
        results = await svc.matchAll("   ", threshold=0.0, limit=10)
        assert results == []

    async def test_matchAll_threshold_filters_low_confidence(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """threshold=0.9 过滤掉所有 Jaccard < 0.9 的结果。"""
        results = await svc.matchAll("供应商", threshold=0.9, limit=10)
        for r in results:
            assert r.confidence >= 0.9

    async def test_matchAll_limit_truncates(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """limit=2 最多返回 2 个结果。"""
        results = await svc.matchAll("供应商", threshold=0.0, limit=2)
        assert len(results) <= 2

    async def test_matchAll_fills_kpi_name(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """matchAll 返回的 KpiMatchResult 填充了 kpi_name。"""
        results = await svc.matchAll("准时", threshold=0.0, limit=10)
        for r in results:
            assert r.kpi_name is not None

    async def test_matchAll_exact_alias_gets_name_from_cache(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """精确 alias 命中的 kpi_name 从缓存获取。"""
        results = await svc.matchAll("KPI_SUPPLIER_OTD", threshold=0.0, limit=10)
        otd = next((r for r in results if r.code == "KPI_SUPPLIER_OTD"), None)
        assert otd is not None
        assert otd.kpi_name == "供应商准时交付率"

    async def test_matchAll_deduplicates_exact_and_jaccard(
        self, svc: KpiSemanticMatchService
    ) -> None:
        """精确 alias 与 Jaccard 命中同一 KPI 时，deduplicate 只保留一条。"""
        # "准时" 既是 Jaccard 候选也可能匹配 alias（如果有 alias）
        results = await svc.matchAll("准时", threshold=0.0, limit=10)
        codes = [r.code for r in results]
        assert len(codes) == len(set(codes))


class TestKpiSearchHitSchema:
    """KpiSearchHit / KpiSearchResponse envelope 验证。"""

    def test_kpi_search_hit_fields(self) -> None:
        """KpiSearchHit 包含 kpi_code / kpi_name / confidence。"""
        from app.domain.schemas import KpiSearchHit

        hit = KpiSearchHit(kpi_code="KPI_X", kpi_name="测试", confidence=0.95)
        assert hit.kpi_code == "KPI_X"
        assert hit.kpi_name == "测试"
        assert hit.confidence == 0.95

    def test_kpi_search_response_envelope(self) -> None:
        """KpiSearchResponse envelope 包含 query + results。"""
        from app.domain.schemas import KpiSearchHit, KpiSearchResponse

        resp = KpiSearchResponse(
            query="准时",
            results=[
                KpiSearchHit(kpi_code="KPI_OTD", kpi_name="供应商准时交付率", confidence=1.0)
            ],
        )
        assert resp.query == "准时"
        assert len(resp.results) == 1
        assert resp.results[0].kpi_code == "KPI_OTD"
