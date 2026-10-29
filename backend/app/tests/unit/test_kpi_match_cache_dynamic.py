"""KpiMatchCache 动态性三洞修复（C1 改名残留 / C2 冷缓存 no-op / C3 绕过）。

真 PG（`dbSession` fixture），不 mock 缓存。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import KpiStatus
from app.domain.models import KpiCatalog
from app.services.kpi_match_cache import KpiMatchCache


async def _seedKpi(
    session: AsyncSession,
    *,
    code: str,
    keywords: list[str],
    status: str = KpiStatus.PUBLISHED.value,
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        business_definition="d",
        formula="SUM(x)",
        numerator="x",
        denominator=None,
        grain="g",
        unit="u",
        data_source="T",
        owner="o",
        version="1",
        status=status,
        metric_id=None,
        created_by="u",
        semantic_keywords=keywords,
        match_threshold=Decimal("0.10"),
    )
    session.add(kpi)
    await session.commit()
    await session.refresh(kpi)
    return kpi


# ── C1：改名残留 ─────────────────────────────────────────────────────


async def test_c1_rename_invalidates_old_code_entry(
    dbSession: AsyncSession,
) -> None:
    """改名后按 code 失效会漏掉旧 code 的残留 ⇒ 必须按 id 失效。"""
    cache = KpiMatchCache()
    kpi = await _seedKpi(
        dbSession, code="KPI_OLD_CODE", keywords=["准时交付"]
    )
    await cache.warmUp(dbSession)
    assert cache.hasCode("KPI_OLD_CODE") is True

    # 改名 + 按 id 失效（service 层 updateKpi 的新调用方式）
    kpi.kpi_code = "KPI_NEW_CODE"
    await dbSession.commit()
    cache.onKpiChanged("KPI_NEW_CODE", kpiId=kpi.id)

    assert cache.hasCode("KPI_OLD_CODE") is False
    assert cache.hasCode("KPI_NEW_CODE") is False  # 新 code 尚未入索引
    # 关键词索引里也不能残留旧条目
    assert cache.findByAnyKeyword(["准时交付"]) == []


async def test_c1_invalidating_by_id_removes_keyword_references(
    dbSession: AsyncSession,
) -> None:
    cache = KpiMatchCache()
    kpi = await _seedKpi(dbSession, code="KPI_KW", keywords=["交付率"])
    await cache.warmUp(dbSession)
    assert len(cache.findByAnyKeyword(["交付率"])) == 1

    cache.onKpiChanged("KPI_KW", kpiId=kpi.id)

    assert cache.findByAnyKeyword(["交付率"]) == []


# ── C2：冷缓存 no-op ─────────────────────────────────────────────────


async def test_c2_refresh_one_on_cold_cache_rebuilds(
    dbSession: AsyncSession,
) -> None:
    """冷缓存下 refreshOne 不得 no-op —— 缓存必须被重建（否则语义匹配永久失效）。"""
    cache = KpiMatchCache()
    await _seedKpi(dbSession, code="KPI_COLD", keywords=["冷启动"])

    assert cache.is_warmUp() is False
    await cache.refreshOne(dbSession, "KPI_COLD")

    assert cache.is_warmUp() is True
    assert cache.hasCode("KPI_COLD") is True
    assert len(cache.findByAnyKeyword(["冷启动"])) == 1


async def test_c2_refresh_one_on_cold_cache_picks_up_other_rows(
    dbSession: AsyncSession,
) -> None:
    """冷缓存重建要看到全量目录（不只是被 refresh 的那一条）。"""
    cache = KpiMatchCache()
    await _seedKpi(dbSession, code="KPI_A", keywords=["甲"])
    await _seedKpi(dbSession, code="KPI_B", keywords=["乙"])

    await cache.refreshOne(dbSession, "KPI_A")

    assert cache.hasCode("KPI_A") is True
    assert cache.hasCode("KPI_B") is True


# ── C3：raw SQL 绕过 ─────────────────────────────────────────────────


async def test_c3_ensure_fresh_rebuilds_after_ttl(
    dbSession: AsyncSession,
) -> None:
    """TTL 到期后 ensureFresh 重建 ⇒ 绕过 ORM listener 的写入也能被读到。"""
    cache = KpiMatchCache()
    await _seedKpi(dbSession, code="KPI_TTL", keywords=["时效"])
    await cache.warmUp(dbSession)

    # 绕过 ORM listener：直接 Core UPDATE（seed 脚本用的就是这条路）
    await dbSession.execute(
        KpiCatalog.__table__.update()
        .where(KpiCatalog.kpi_code == "KPI_TTL")
        .values(semantic_keywords=["时效", "新关键词"])
    )
    await dbSession.commit()

    # TTL=0 ⇒ 立即判定过期
    await cache.ensureFresh(dbSession, maxAgeSeconds=0)

    assert len(cache.findByAnyKeyword(["新关键词"])) == 1


async def test_c3_ensure_fresh_rebuilds_when_dirty(
    dbSession: AsyncSession,
) -> None:
    """脏标记（写路径失效过）⇒ ensureFresh 重建。"""
    cache = KpiMatchCache()
    await _seedKpi(dbSession, code="KPI_DIRTY", keywords=["脏标记"])
    await cache.warmUp(dbSession)
    assert cache.hasCode("KPI_DIRTY") is True

    cache.onKpiChanged(None)  # 全量失效
    await cache.ensureFresh(dbSession)

    assert cache.hasCode("KPI_DIRTY") is True


async def test_c3_ensure_fresh_is_noop_when_fresh(
    dbSession: AsyncSession,
) -> None:
    """缓存新鲜时 ensureFresh 不重建（不产生多余查询）。"""
    cache = KpiMatchCache()
    await _seedKpi(dbSession, code="KPI_FRESH", keywords=["新鲜"])
    await cache.warmUp(dbSession)

    calls = {"n": 0}
    original = cache.warmUp

    async def _counting(session):
        calls["n"] += 1
        await original(session)

    cache.warmUp = _counting  # type: ignore[method-assign]
    await cache.ensureFresh(dbSession, maxAgeSeconds=3600)
    assert calls["n"] == 0
