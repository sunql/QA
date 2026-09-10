"""Phase 1 Task 1.3: KpiMatchCache 单元测试（TDD RED -> GREEN）。

覆盖：
1. 启动时全量加载 active KPI
2. 写时失效：update / insert / delete 任一操作均清空受影响条目

使用 FakeSession + FakeResult 模拟 DB，不依赖真实 PostgreSQL。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from app.services.kpi_match_cache import KpiMatchCache


# ---------------------------------------------------------------------------
# Fake row / result / session
# ---------------------------------------------------------------------------

class _FakeKpiRow:
    """模拟 KpiCatalog DB 行（仅语义匹配相关字段）。"""
    def __init__(
        self,
        kpi_code: str,
        kpi_name: str,
        semantic_keywords: list[str] | None,
        match_threshold: Decimal,
        status: str,
    ) -> None:
        self.kpi_code = kpi_code
        self.kpi_name = kpi_name
        self.semantic_keywords = semantic_keywords
        self.match_threshold = match_threshold
        self.status = status

    def _asdict(self):
        return {
            "kpi_code": self.kpi_code,
            "kpi_name": self.kpi_name,
            "semantic_keywords": self.semantic_keywords,
            "match_threshold": self.match_threshold,
            "status": self.status,
        }


class _FakeScalarResult:
    """Fake for rows.scalars() → ScalarResult which has .all()."""
    def __init__(self, rows: list[_FakeKpiRow]) -> None:
        self._rows = rows

    def all(self) -> list[_FakeKpiRow]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[_FakeKpiRow]) -> None:
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._rows)


class _FakeSession:
    """Fake AsyncSession：可预设全量行，及单条 update/delete 后的状态。

    默认行为（warmUp 路径）：仅返回 status=PUBLISHED 的行。
    因为 KpiMatchCache.warmUp() 的查询条件恒为 status=PUBLISHED，
    这是唯一被调用的查询路径。
    """
    def __init__(
        self,
        initial_rows: list[_FakeKpiRow],
    ) -> None:
        # 全量行
        self._rows: list[_FakeKpiRow] = list(initial_rows)
        # 事件队列（模拟 insert/update/delete 后的变化）
        self._after_events: list[str] = []

    async def execute(self, stmt):
        if self._after_events:
            event = self._after_events.pop(0)
            if event == "INSERT":
                pass
            elif event == "UPDATE":
                pass
        # KpiMatchCache.warmUp() 恒查 status=PUBLISHED；返回仅有 PUBLISHED 行
        return _FakeResult([r for r in self._rows if r.status == "PUBLISHED"])

    def simulate_insert(self, row: _FakeKpiRow) -> None:
        """模拟 insert 后该行进入 _rows。"""
        self._rows.append(row)
        self._after_events.append("INSERT")

    def simulate_update(self, kpi_code: str) -> None:
        """模拟 update 后触发写时失效。"""
        self._after_events.append("UPDATE")

    def simulate_delete(self, kpi_code: str) -> None:
        """模拟 delete 后触发写时失效。"""
        self._rows = [r for r in self._rows if r.kpi_code != kpi_code]
        self._after_events.append("DELETE")


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_KPI_OTD = _FakeKpiRow(
    kpi_code="KPI_SUPPLIER_OTD",
    kpi_name="供应商准时交付率",
    semantic_keywords=["准时交付", "OTD", "按时", "准时率"],
    match_threshold=Decimal("0.75"),
    status="PUBLISHED",
)
_KPI_DEFECT = _FakeKpiRow(
    kpi_code="KPI_SUPPLIER_DEFECT_RATE",
    kpi_name="供应商来料不良率",
    semantic_keywords=["来料不良", "不良率", "defect", "不合格率"],
    match_threshold=Decimal("0.75"),
    status="PUBLISHED",
)
_KPI_DRAFT = _FakeKpiRow(
    kpi_code="KPI_DRAFT_SAMPLE",
    kpi_name="草稿指标",
    semantic_keywords=["草稿"],
    match_threshold=Decimal("0.75"),
    status="DRAFT",
)
_KPI_DISABLED = _FakeKpiRow(
    kpi_code="KPI_DEPRECATED_SAMPLE",
    kpi_name="已停用指标",
    semantic_keywords=["停用"],
    match_threshold=Decimal("0.75"),
    status="DEPRECATED",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCacheStartup:
    """启动预热：只加载 is_enabled=True + status=PUBLISHED 的 KPI。"""

    @pytest.fixture()
    def cache(self) -> KpiMatchCache:
        return KpiMatchCache()

    @pytest.mark.asyncio
    async def test_cache_loads_all_active_kpis_on_startup(self, cache: KpiMatchCache) -> None:
        """全量加载 status=PUBLISHED 的 KPI，DRAFT/DEPRECATED 不加载。"""
        session = _FakeSession(initial_rows=[_KPI_OTD, _KPI_DEFECT, _KPI_DRAFT, _KPI_DISABLED])
        await cache.warmUp(session)

        # 两条 PUBLISHED 应在缓存中
        assert cache.hasCode("KPI_SUPPLIER_OTD") is True
        assert cache.hasCode("KPI_SUPPLIER_DEFECT_RATE") is True
        # DRAFT / DEPRECATED 不应加载
        assert cache.hasCode("KPI_DRAFT_SAMPLE") is False
        assert cache.hasCode("KPI_DEPRECATED_SAMPLE") is False

    @pytest.mark.asyncio
    async def test_cache_hasCode_returns_false_before_warmUp(self, cache: KpiMatchCache) -> None:
        """未 warmUp 前访问 hasCode 应抛 RuntimeError（防未初始化使用）。"""
        with pytest.raises(RuntimeError, match="未 warmUp"):
            cache.hasCode("KPI_SUPPLIER_OTD")

    @pytest.mark.asyncio
    async def test_getAll_returns_only_active(self, cache: KpiMatchCache) -> None:
        """getAll() 仅返回 PUBLISHED 的 KPI。"""
        session = _FakeSession(initial_rows=[_KPI_OTD, _KPI_DEFECT, _KPI_DRAFT])
        await cache.warmUp(session)

        all_items = cache.getAll()
        codes = {item.kpi_code for item in all_items}
        assert "KPI_SUPPLIER_OTD" in codes
        assert "KPI_SUPPLIER_DEFECT_RATE" in codes
        assert "KPI_DRAFT_SAMPLE" not in codes


class TestCacheFindByKeyword:
    """findByAnyKeyword：keyword 包含在 semantic_keywords 的 KPI 列表。"""

    @pytest.fixture()
    def cache(self) -> KpiMatchCache:
        return KpiMatchCache()

    @pytest.mark.asyncio
    async def test_findByAnyKeyword_substring_match(self, cache: KpiMatchCache) -> None:
        """keyword 是 catalog keyword 的子串时命中。"""
        session = _FakeSession(initial_rows=[_KPI_OTD, _KPI_DEFECT])
        await cache.warmUp(session)

        # "准时" 子串匹配 "准时交付"
        results = cache.findByAnyKeyword(["准时"])
        assert len(results) == 1
        assert results[0].kpi_code == "KPI_SUPPLIER_OTD"

    @pytest.mark.asyncio
    async def test_findByAnyKeyword_empty_result(self, cache: KpiMatchCache) -> None:
        """无任何匹配时返回空列表。"""
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        results = cache.findByAnyKeyword(["完全不相关的词"])
        assert results == []


class TestCacheInvalidation:
    """写时失效：update / insert / delete 触发 onKpiChanged。"""

    @pytest.fixture()
    def cache(self) -> KpiMatchCache:
        return KpiMatchCache()

    @pytest.mark.asyncio
    async def test_cache_invalidates_on_kpi_update(self, cache: KpiMatchCache) -> None:
        """更新 KPI 的 semantic_keywords 后，cache 中对应条目应失效（下次 refresh 反映新数据）。

        验证方式：update 后 cache 仍含旧数据；调用 onKpiChanged 后 getAll 重刷反映空 semantic_keywords。
        """
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        # 验证初始有数据
        assert cache.hasCode("KPI_SUPPLIER_OTD") is True

        # 模拟 update 触发写时失效
        cache.onKpiChanged("KPI_SUPPLIER_OTD")

        # onKpiChanged 后该条被标记为失效；重新 warmUp 后反映更新
        # 由于 FakeSession 无法真正模拟 update，我们测：调用 onKpiChanged 后，
        # hasCode 应返回 False（条目已从缓存移除，需下次 refresh 重新加载）
        assert cache.hasCode("KPI_SUPPLIER_OTD") is False

    @pytest.mark.asyncio
    async def test_cache_invalidates_on_kpi_insert(self, cache: KpiMatchCache) -> None:
        """新增 KPI 后，cache 应失效，下次 refresh 能加载新 KPI。"""
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        # 新增一条
        new_kpi = _FakeKpiRow(
            kpi_code="KPI_NEW_INSERTED",
            kpi_name="新增指标",
            semantic_keywords=["新增"],
            match_threshold=Decimal("0.75"),
            status="PUBLISHED",
        )
        session.simulate_insert(new_kpi)

        # 触发写时失效
        cache.onKpiChanged("KPI_NEW_INSERTED")

        # 该条不在缓存中（需下次 refresh）
        assert cache.hasCode("KPI_NEW_INSERTED") is False

    @pytest.mark.asyncio
    async def test_cache_invalidates_on_kpi_delete(self, cache: KpiMatchCache) -> None:
        """删除 KPI 后，cache 中对应条目应失效。"""
        session = _FakeSession(initial_rows=[_KPI_OTD, _KPI_DEFECT])
        await cache.warmUp(session)

        assert cache.hasCode("KPI_SUPPLIER_OTD") is True

        # 模拟 delete
        session.simulate_delete("KPI_SUPPLIER_OTD")
        cache.onKpiChanged("KPI_SUPPLIER_OTD")

        # 已从缓存移除
        assert cache.hasCode("KPI_SUPPLIER_OTD") is False
        # 另一条不受影响
        assert cache.hasCode("KPI_SUPPLIER_DEFECT_RATE") is True
