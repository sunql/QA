"""KpiMatchCache 动态性三洞修复（C1 改名残留 / C2 冷缓存 no-op / C3 绕过）。

真 PG（`dbSession` fixture），不 mock 缓存。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
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
    """按 id 失效必须摘掉两个索引里的**全部**引用。

    区分性：这里传入的 code 与缓存里的 key **不同** —— 改名残留场景（DB 已是新
    code，缓存 key 仍是旧 code，写路径手里只有 id）。只按 code 失效的实现
    （`pop(code)` + 按 code 过滤关键词桶）**摘不掉**旧条目 ⇒ 本用例必须红。
    """
    cache = KpiMatchCache()
    kpi = await _seedKpi(dbSession, code="KPI_KW", keywords=["交付率"])
    await cache.warmUp(dbSession)
    assert len(cache.findByAnyKeyword(["交付率"])) == 1

    # 改名残留：缓存 key 仍是旧 code，写路径只拿到 id
    cache.onKpiChanged("KPI_KW_RENAMED", kpiId=kpi.id)

    assert cache.hasCode("KPI_KW") is False
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


# ── 区分性覆盖：listener 真失效 / _dirty 分支 / populate_existing 条件化 ────


async def test_c3_orm_listener_really_invalidates_cache(
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ORM 写入必须**真失效**缓存条目（listener 不是只 log）。

    listener 固定作用于 `get_kpi_match_cache()` 返回的对象 ⇒ 把该访问器
    monkeypatch 成返回**局部**实例，才能观察到 listener 的效果（否则断言看的是
    另一个对象，删掉 listener 的真失效行为测试仍然全绿）。
    """
    from app.services import kpi_match_cache as cacheModule

    globalCache = cacheModule.get_kpi_match_cache()
    wasLoaded = globalCache.is_warmUp()

    local = KpiMatchCache()
    monkeypatch.setattr(cacheModule, "get_kpi_match_cache", lambda: local)

    kpi = await _seedKpi(dbSession, code="KPI_LISTENER", keywords=["监听"])
    await local.warmUp(dbSession)
    assert local.hasCode("KPI_LISTENER") is True
    assert len(local.findByAnyKeyword(["监听"])) == 1

    # ORM 写入 ⇒ flush ⇒ after_update listener ⇒ 真失效
    kpi.semantic_keywords = ["监听", "新口径"]
    await dbSession.commit()

    assert local.hasCode("KPI_LISTENER") is False
    assert local.findByAnyKeyword(["监听"]) == []

    # 复位：局部实例清空；全局单例恢复到用例前状态（避免污染同进程后续用例）
    local.onKpiChanged(None)
    if wasLoaded:
        await globalCache.warmUp(dbSession)
    else:
        globalCache.onKpiChanged(None)


async def test_c3_ensure_fresh_rebuilds_when_dirty_by_local_invalidation(
    dbSession: AsyncSession,
) -> None:
    """**局部**失效（`_loaded` 仍为 True）⇒ ensureFresh 必须重建。

    区分性：全量失效 `onKpiChanged(None)` 会把 `_loaded` 置 False ⇒ 命中的是
    「冷缓存」分支，与 `_dirty` 无关。本用例走 `_dirty` 分支：只有 `_dirty`
    判定存在时才会重建；把它去掉（或恒 False）本用例必须红。
    """
    cache = KpiMatchCache()
    kpiA = await _seedKpi(dbSession, code="KPI_A", keywords=["甲"])
    await _seedKpi(dbSession, code="KPI_B", keywords=["乙"])
    await cache.warmUp(dbSession)
    assert cache.hasCode("KPI_A") is True

    # 局部失效：_loaded 仍为 True，只标脏
    cache.onKpiChanged("KPI_A", kpiId=kpiA.id)
    assert cache.is_warmUp() is True
    assert cache.hasCode("KPI_A") is False

    # DB 侧改动：绕过该缓存与 ORM listener（Core UPDATE）
    await dbSession.execute(
        KpiCatalog.__table__.update()
        .where(KpiCatalog.kpi_code == "KPI_B")
        .values(semantic_keywords=["乙", "新乙"])
    )
    await dbSession.commit()

    # TTL 很长 ⇒ 唯一能触发重建的就是 _dirty 分支
    await cache.ensureFresh(dbSession, maxAgeSeconds=3600)

    assert len(cache.findByAnyKeyword(["新乙"])) == 1
    # 重建同时把被局部失效的条目补回来
    assert cache.hasCode("KPI_A") is True


async def test_c3_warm_up_does_not_overwrite_pending_session_changes(
    dbSession: AsyncSession,
) -> None:
    """session 里有**未 flush** 的 KpiCatalog 改动时，warmUp 不得抹掉它。

    `populate_existing=True` 会用查询结果覆盖 identity map 里同 id 实例的属性
    ⇒ 会把调用方未落库的改动静默改回去（生产 session `autoflush=False` ⇒ 随后的
    flush 把旧值写回 DB = 静默数据丢失）。故 warmUp 必须**条件化**该刷新语义。
    """
    cache = KpiMatchCache()
    kpi = await _seedKpi(dbSession, code="KPI_PENDING", keywords=["待落库"])

    # 只改内存、不 flush
    kpi.kpi_name = "内存里改过的名字"
    assert kpi in dbSession.dirty

    await cache.warmUp(dbSession)

    assert kpi.kpi_name == "内存里改过的名字"
