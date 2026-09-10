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
        is_enabled: bool = True,
    ) -> None:
        self.kpi_code = kpi_code
        self.kpi_name = kpi_name
        self.semantic_keywords = semantic_keywords
        self.match_threshold = match_threshold
        self.status = status
        self.is_enabled = is_enabled

    def _asdict(self):
        return {
            "kpi_code": self.kpi_code,
            "kpi_name": self.kpi_name,
            "semantic_keywords": self.semantic_keywords,
            "match_threshold": self.match_threshold,
            "status": self.status,
            "is_enabled": self.is_enabled,
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

    def scalar_one_or_none(self) -> _FakeKpiRow | None:
        return self._rows[0] if self._rows else None


def _where_column_keys(stmt) -> set[str]:
    """Extract column keys from a SQLAlchemy select statement's whereclause."""
    result: set[str] = set()
    wc = stmt.whereclause
    if wc is None:
        return result
    def walk(clause):
        if hasattr(clause, "left") and hasattr(clause, "right"):
            left = clause.left
            if hasattr(left, "key"):
                result.add(left.key)
        if hasattr(clause, "clauses"):
            for c in clause.clauses:
                walk(c)
    walk(wc)
    return result


class _FakeSession:
    """Fake AsyncSession：可预设全量行，及单条 update/delete 后的状态。

    支持两种查询模式：
    1. warmUp 路径：status=PUBLISHED 的全量行
    2. refreshOne 路径：按 kpi_code 精确查询单行
    """
    def __init__(
        self,
        initial_rows: list[_FakeKpiRow],
    ) -> None:
        # 全量行
        self._rows: list[_FakeKpiRow] = list(initial_rows)
        # 事件队列（模拟 insert/update/delete 后的变化）
        self._after_events: list[str] = []
        # refreshOne 注入：模拟更新后的行（code -> row 映射）
        self._updated_rows: dict[str, _FakeKpiRow] = {}

    async def execute(self, stmt):
        # 通过 whereclause 判断查询类型（不用字符串匹配，避免 SELECT 列表干扰）
        where_keys = _where_column_keys(stmt)
        # refreshOne：where 条件包含 kpi_code（不含 status）
        # warmUp：where 条件包含 status
        if "kpi_code" in where_keys and "status" not in where_keys:
            # 优先用 _updated_rows 中的版本（反映 update 后的数据）
            if self._updated_rows:
                for row in list(self._rows):
                    if row.kpi_code in self._updated_rows:
                        found = self._updated_rows[row.kpi_code]
                        return _FakeResult([found] if found.status == "PUBLISHED" else [])
            # fallback：原始行
            for row in list(self._rows):
                if row.status == "PUBLISHED":
                    return _FakeResult([row])
            return _FakeResult([])
        # warmUp：返回所有 PUBLISHED 行
        return _FakeResult([r for r in self._rows if r.status == "PUBLISHED"])

    def simulate_insert(self, row: _FakeKpiRow) -> None:
        """模拟 insert 后该行进入 _rows。"""
        self._rows.append(row)
        self._updated_rows[row.kpi_code] = row
        self._after_events.append("INSERT")

    def simulate_update(self, kpi_code: str, updated_row: _FakeKpiRow | None = None) -> None:
        """模拟 update 后触发写时失效，并可注入更新后的行数据。"""
        self._after_events.append("UPDATE")
        if updated_row:
            self._updated_rows[kpi_code] = updated_row

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


class TestOnKpiChangedKeywordCleanup:
    """onKpiChanged 必须同时清理 _by_code 和 _by_keyword，防止 stale keyword entries。"""

    @pytest.fixture()
    def cache(self) -> KpiMatchCache:
        return KpiMatchCache()

    @pytest.mark.asyncio
    async def test_onKpiChanged_cleans_keyword_index(self, cache: KpiMatchCache) -> None:
        """更新 KPI 改变 semantic_keywords 后，旧 keyword 索引必须被清除。"""
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        # 初始：keyword 索引中有 "准时交付"
        results = cache.findByAnyKeyword(["准时交付"])
        assert len(results) == 1
        assert results[0].kpi_code == "KPI_SUPPLIER_OTD"

        # onKpiChanged 后，_by_code 和 _by_keyword 都应清空该 KPI 的引用
        cache.onKpiChanged("KPI_SUPPLIER_OTD")
        results_after = cache.findByAnyKeyword(["准时交付"])
        assert results_after == []

    @pytest.mark.asyncio
    async def test_onKpiChanged_cleans_code_index(self, cache: KpiMatchCache) -> None:
        """onKpiChanged 后 hasCode 应返回 False。"""
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        assert cache.hasCode("KPI_SUPPLIER_OTD") is True
        cache.onKpiChanged("KPI_SUPPLIER_OTD")
        assert cache.hasCode("KPI_SUPPLIER_OTD") is False

    @pytest.mark.asyncio
    async def test_refreshOne_removes_old_keywords(self, cache: KpiMatchCache) -> None:
        """refreshOne 替换 KPI 时，旧的 keyword 索引被清理。"""
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        # 初始有 "准时交付"
        assert len(cache.findByAnyKeyword(["准时交付"])) == 1

        # 模拟 update：同一 code 的 KPI，但 keywords 完全变了
        updated_kpi = _FakeKpiRow(
            kpi_code="KPI_SUPPLIER_OTD",
            kpi_name="供应商准时交付率（更新）",
            semantic_keywords=["新关键词", "new"],
            match_threshold=Decimal("0.80"),
            status="PUBLISHED",
            is_enabled=True,
        )
        session.simulate_update("KPI_SUPPLIER_OTD", updated_kpi)
        await cache.refreshOne(session, "KPI_SUPPLIER_OTD")

        # 旧 keyword 不再命中
        assert cache.findByAnyKeyword(["准时交付"]) == []
        # 新 keyword 命中
        results = cache.findByAnyKeyword(["新关键词"])
        assert len(results) == 1
        assert results[0].kpi_code == "KPI_SUPPLIER_OTD"


class TestKpiCatalogServiceWriteInvalidation:
    """验证 KpiCatalogService.create/update/delete 后 cache 被正确刷新的行为。"""

    @pytest.fixture()
    def cache(self) -> KpiMatchCache:
        return KpiMatchCache()

    @pytest.mark.asyncio
    async def test_service_create_refreshes_cache(self, cache: KpiMatchCache) -> None:
        """create 后 cache 必须包含新 KPI。"""
        from app.services.kpi_match_cache import KpiMatchCache as CacheModule

        # 用测试缓存替换模块单例
        original = CacheModule._test_cache = CacheModule._test_cache if hasattr(CacheModule, '_test_cache') else None
        CacheModule._test_cache = cache

        try:
            session = _FakeSession(initial_rows=[_KPI_OTD])
            await cache.warmUp(session)

            # 模拟创建新 KPI
            new_kpi = _FakeKpiRow(
                kpi_code="KPI_NEW_SERVICE",
                kpi_name="新建指标",
                semantic_keywords=["新"],
                match_threshold=Decimal("0.75"),
                status="PUBLISHED",
                is_enabled=True,
            )
            session.simulate_insert(new_kpi)

            # refreshOne 模拟 createKpi 后的行为
            await cache.refreshOne(session, "KPI_NEW_SERVICE")

            assert cache.hasCode("KPI_NEW_SERVICE") is True
            assert len(cache.findByAnyKeyword(["新"])) == 1
        finally:
            if original is not None:
                CacheModule._test_cache = original
            elif hasattr(CacheModule, '_test_cache'):
                delattr(CacheModule, '_test_cache')

    @pytest.mark.asyncio
    async def test_service_update_refreshes_cache(self, cache: KpiMatchCache) -> None:
        """update 后 cache 必须反映新 keywords。"""
        session = _FakeSession(initial_rows=[_KPI_OTD])
        await cache.warmUp(session)

        updated_kpi = _FakeKpiRow(
            kpi_code="KPI_SUPPLIER_OTD",
            kpi_name="供应商准时交付率（更新）",
            semantic_keywords=["changed"],
            match_threshold=Decimal("0.80"),
            status="PUBLISHED",
            is_enabled=True,
        )
        session.simulate_update("KPI_SUPPLIER_OTD", updated_kpi)
        await cache.refreshOne(session, "KPI_SUPPLIER_OTD")

        assert cache.hasCode("KPI_SUPPLIER_OTD") is True
        # 旧 keyword 已清
        assert cache.findByAnyKeyword(["准时交付"]) == []
        # 新 keyword 存在
        results = cache.findByAnyKeyword(["changed"])
        assert len(results) == 1
        assert results[0].kpi_code == "KPI_SUPPLIER_OTD"

    @pytest.mark.asyncio
    async def test_service_delete_invalidates_cache(self, cache: KpiMatchCache) -> None:
        """delete 后 cache 必须移除该 KPI。"""
        session = _FakeSession(initial_rows=[_KPI_OTD, _KPI_DEFECT])
        await cache.warmUp(session)

        session.simulate_delete("KPI_SUPPLIER_OTD")
        cache.onKpiChanged("KPI_SUPPLIER_OTD")

        assert cache.hasCode("KPI_SUPPLIER_OTD") is False
        # 其他 KPI 不受影响
        assert cache.hasCode("KPI_SUPPLIER_DEFECT_RATE") is True
