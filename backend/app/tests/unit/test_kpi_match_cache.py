"""KpiMatchCache 真实实现的差分属性测试（M10）。

背景（`Harness/wiki/chat-service-assessment.md` §2.3 M10、SSOT
`Harness/changes/test-kpi-match-cache-ordering/`）：本条目原计划把 `_by_keyword`
的线性子串扫描换成倒排索引，**实测后改判为放弃** —— 真实目录只有 13 个 KPI
（约 50 个关键词），线性扫描本就是亚毫秒级，换不来任何**可测**收益；而索引会引入
两处静默偏离（`""` 空关键词语义、写路径不重建索引导致陈旧命中 + 顺序漂移），
500×30 字实测 +42.3 MB 不 scale。

本条目真正缺的是**覆盖**：现有测试用的都是**自行重写了一套不同算法的桩**
（`test_kpi_semantic_match_service.py::_StubCache`、`test_kpi_catalog_api.py` 的 fake
都是外层遍历 KPI、命中即 `break`，按目录序），而真实实现的顺序是
「**用户关键词外层 × `_by_keyword` 插入序内层**」⇒ **真实顺序零覆盖**。

本文件对真实 `KpiMatchCache` 做**差分**校验：测试侧用朴素实现独立复算「文档化的
匹配规则」与「文档化的顺序规则」（`_expectedHitSet` / `_expectedOrder`），再与缓存
输出逐条比对 —— 而不是把缓存的输出抄一遍当期望值（那样同义反复，测不出任何东西）。
任何未来的顺序漂移或集合漂移都会立刻转红。

**不改任何生产代码** ⇒ 纯 `test:` 变更，零运行时风险。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import KpiStatus
from app.domain.models import KpiCatalog
from app.services.kpi_match_cache import KpiMatchCache

# ---------------------------------------------------------------------------
# 测试侧独立复算（oracle）—— 刻意用朴素写法，与被测实现不共享代码
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _KpiSpec:
    """一条待插入的 KPI 目录行（`status` 决定是否该入索引）。"""

    code: str
    keywords: list[str]
    status: KpiStatus = KpiStatus.PUBLISHED


def _catalogKeywordOrder(specs: tuple[_KpiSpec, ...]) -> dict[str, list[str]]:
    """复算 `_by_keyword` 的**插入序**：按 KPI 顺序、关键词顺序首次出现。

    这是文档化顺序规则的一半（另一半是用户关键词顺序），独立于被测实现。
    """
    byKeyword: dict[str, list[str]] = {}
    for spec in specs:
        if spec.status is not KpiStatus.PUBLISHED:
            continue
        for kw in spec.keywords:
            byKeyword.setdefault(kw, []).append(spec.code)
    return byKeyword


def _expectedHitSet(specs: tuple[_KpiSpec, ...], keywords: list[str]) -> set[str]:
    """复算**集合**语义：任一用户关键词是任一目录关键词的子串（大小写不敏感）。

    纯集合（不带顺序），因此不含任何实现细节假设 —— 现有桩若语义写错（例如漏掉
    大小写归一化、或按整词而非子串匹配），这里立刻暴露。
    """
    byKeyword = _catalogKeywordOrder(specs)
    lowered = [kw.lower() for kw in keywords]
    hits: set[str] = set()
    for catKw, codes in byKeyword.items():
        if any(query in catKw.lower() for query in lowered):
            hits.update(codes)
    return hits


def _expectedOrder(specs: tuple[_KpiSpec, ...], keywords: list[str]) -> list[str]:
    """复算**顺序**语义：用户关键词顺序外层 × `_by_keyword` 插入序内层，按 code 去重。

    与 `findByAnyKeyword` 的循环结构同构，但跑在测试自己的 spec 列表上 ⇒ 非自证。
    """
    byKeyword = _catalogKeywordOrder(specs)
    seen: set[str] = set()
    ordered: list[str] = []
    for kw in keywords:
        kwLower = kw.lower()
        for catKw, codes in byKeyword.items():
            if kwLower in catKw.lower():
                for code in codes:
                    if code not in seen:
                        seen.add(code)
                        ordered.append(code)
    return ordered


def _codes(kpis: list[KpiCatalog]) -> list[str]:
    return [kpi.kpi_code for kpi in kpis]


def _assertMatchesByRule(
    cache: KpiMatchCache,
    specs: tuple[_KpiSpec, ...],
    keywords: list[str],
) -> list[str]:
    """共享断言：集合与顺序都按文档化规则复算，任何漂移立刻转红。

    返回实际 code 序列，便于用例追加更细的断言。
    """
    actual = _codes(cache.findByAnyKeyword(keywords))
    assert set(actual) == _expectedHitSet(specs, keywords), (
        f"命中集合漂移：keywords={keywords!r} actual={actual!r}"
    )
    assert actual == _expectedOrder(specs, keywords), (
        f"命中顺序漂移：keywords={keywords!r} actual={actual!r} "
        f"expected={_expectedOrder(specs, keywords)!r}"
    )
    return actual


async def _insertKpis(session: AsyncSession, specs: tuple[_KpiSpec, ...]) -> None:
    """插入目录行并提交（`warmUp` 只看 PUBLISHED）。"""
    for spec in specs:
        session.add(
            KpiCatalog(
                kpi_code=spec.code,
                kpi_name=f"{spec.code} 名称",
                status=spec.status.value,
                semantic_keywords=list(spec.keywords),
            )
        )
    await session.commit()


async def _warmUp(session: AsyncSession, specs: tuple[_KpiSpec, ...]) -> KpiMatchCache:
    """插数据 + warmUp，返回**独立实例**（不碰模块级单例，用例互不污染）。"""
    await _insertKpis(session, specs)
    cache = KpiMatchCache()
    await cache.warmUp(session)
    assert cache.is_warmUp() is True
    return cache


# 交叠关键词语料。**顺序是刻意排的**：`销售额增长率` 是 `销售额` 的超串（两者都命中
# 查询词 `销售额`），且它**先**被索引 ⇒ 「按 `_by_keyword` 插入序」给出的
# [KPI_GROWTH, KPI_SALES] 与「按关键词码点排序」给出的 [KPI_SALES, KPI_GROWTH] **不同**。
# 若把语料排成插入序 == 排序序，顺序断言就成了摆设（任何排序实现都能通过）。
_OVERLAP: tuple[_KpiSpec, ...] = (
    _KpiSpec("KPI_GROWTH", ["销售额增长率"]),
    _KpiSpec("KPI_SALES", ["销售额"]),
    _KpiSpec("KPI_OTD", ["准时交付率", "供应商"]),
)


# ---------------------------------------------------------------------------
# findByAnyKeyword：匹配与顺序
# ---------------------------------------------------------------------------


class TestFindByAnyKeyword:
    """`findByAnyKeyword` 的集合语义与顺序语义。

    注：`warmUp` 的 SELECT **没有 ORDER BY**，因此 `_by_keyword` 的插入序即数据库
    返回序。测试库每用例 TRUNCATE 后按插入序写堆 ⇒ PG 顺序扫描返回插入序。这是
    当前实现隐含依赖的性质：本文件把它**显式钉死**（顺序真的漂了，这里是第一个报警点）。
    """

    async def test_overlapping_keywords_follow_user_then_catalog_order(
        self, dbSession: AsyncSession
    ) -> None:
        """交叠关键词：`["销售额"]` 同时命中两个 KPI，顺序 = 用户词序 × 目录词**插入**序。

        语料刻意为「插入序 ≠ 排序序」（见 `_OVERLAP` 注释）：期望值 [GROWTH, SALES]
        恰好是排序实现会给出的 [SALES, GROWTH] 的反面 ⇒ 本断言真的在钉插入序。
        """
        cache = await _warmUp(dbSession, _OVERLAP)
        assert _assertMatchesByRule(cache, _OVERLAP, ["销售额"]) == [
            "KPI_GROWTH",
            "KPI_SALES",
        ]

    async def test_multi_keyword_dedupes_without_reordering(
        self, dbSession: AsyncSession
    ) -> None:
        """多个用户关键词：重复词不产生重复项，且先出现的用户词排在前。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        actual = _assertMatchesByRule(cache, _OVERLAP, ["销售额增长率", "销售额", "销售额"])
        assert actual == ["KPI_GROWTH", "KPI_SALES"]
        assert len(actual) == len(set(actual)), "同一 KPI 不得因多个关键词出现两次"

    async def test_generic_keyword_hits_every_carrier(
        self, dbSession: AsyncSession
    ) -> None:
        """通用词命中所有携带它的 KPI（`供应商` 只属于 KPI_OTD）。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        assert _assertMatchesByRule(cache, _OVERLAP, ["供应商"]) == ["KPI_OTD"]

    async def test_case_insensitive_matching(self, dbSession: AsyncSession) -> None:
        """大小写不敏感（用户词与目录词两侧都要归一化）。"""
        specs: tuple[_KpiSpec, ...] = (_KpiSpec("KPI_EN", ["OnTimeDelivery"]),)
        cache = await _warmUp(dbSession, specs)
        for query in ("ontime", "ONTIMEDELIVERY", "OnTimeDelivery"):
            assert _assertMatchesByRule(cache, specs, [query]) == ["KPI_EN"], query

    async def test_returns_empty_when_nothing_matches(self, dbSession: AsyncSession) -> None:
        """无命中返回空列表（不是 None、不抛错）。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        assert _assertMatchesByRule(cache, _OVERLAP, ["毛利率"]) == []
        assert _assertMatchesByRule(cache, _OVERLAP, []) == []

    async def test_draft_kpis_are_never_indexed(self, dbSession: AsyncSession) -> None:
        """只有 PUBLISHED 入索引（与 warmUp 同口径）。"""
        specs: tuple[_KpiSpec, ...] = (
            _KpiSpec("KPI_LIVE", ["销售额"]),
            _KpiSpec("KPI_WIP", ["销售额"], status=KpiStatus.DRAFT),
        )
        cache = await _warmUp(dbSession, specs)
        assert cache.hasCode("KPI_LIVE") is True
        assert cache.hasCode("KPI_WIP") is False
        assert _assertMatchesByRule(cache, specs, ["销售额"]) == ["KPI_LIVE"]

    async def test_empty_keyword_matches_every_published_kpi(
        self, dbSession: AsyncSession
    ) -> None:
        """**钉死当前语义**：空字符串是任意字符串的子串 ⇒ 命中全部 PUBLISHED KPI。

        这是既有行为而不是本次判定 —— 若将来判定为缺陷（用户传 `[""]` 不该等同于
        “全量”），需另开条目显式改，**不要顺手改**：改它等于改 `findByAnyKeyword`
        对空词的契约，而 `KpiSemanticMatchService` 的调用方依赖当前语义做降级。
        """
        cache = await _warmUp(dbSession, _OVERLAP)
        assert _assertMatchesByRule(cache, _OVERLAP, [""]) == _expectedOrder(_OVERLAP, [""])
        assert set(_codes(cache.findByAnyKeyword([""]))) == {
            "KPI_GROWTH",
            "KPI_SALES",
            "KPI_OTD",
        }

    async def test_never_returns_the_same_instance_twice(
        self, dbSession: AsyncSession
    ) -> None:
        """去重是按**实例 identity** 做的（`id(kpi)`）⇒ 结果里不得有重复 identity。

        一个 KPI 被多个关键词命中时必须是同一个对象，不能被复制成两份。
        """
        specs: tuple[_KpiSpec, ...] = (_KpiSpec("KPI_DUP", ["销售额", "销售额增长率"]),)
        cache = await _warmUp(dbSession, specs)
        result = cache.findByAnyKeyword(["销售额", "增长"])
        assert _codes(result) == ["KPI_DUP"]
        assert len({id(kpi) for kpi in result}) == 1


# ---------------------------------------------------------------------------
# 写路径失效：onKpiChanged / refreshOne
# ---------------------------------------------------------------------------


class TestWritePathInvalidation:
    """写路径（`onKpiChanged` / `refreshOne`）只改 `_by_keyword`，不改其它索引。"""

    async def test_onKpiChanged_removes_only_that_kpi(self, dbSession: AsyncSession) -> None:
        """单条失效：只摘掉指定 KPI，其它 KPI 与顺序不变。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        cache.onKpiChanged("KPI_SALES")
        assert cache.hasCode("KPI_SALES") is False
        assert cache.hasCode("KPI_OTD") is True
        # `KPI_SALES` 的 `销售额` 桶清空后被 `del`，但 `销售额增长率` 桶仍在
        # ⇒ 该 KPI 彻底摘除，另一条命中不受影响
        assert _codes(cache.findByAnyKeyword(["销售额"])) == ["KPI_GROWTH"]

    async def test_onKpiChanged_none_invalidates_everything(self, dbSession: AsyncSession) -> None:
        """全量失效：`_loaded` 归位 False，读路径全部降级（不抛错、不返回陈旧数据）。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        cache.onKpiChanged(None)
        assert cache.is_warmUp() is False
        assert cache.findByAnyKeyword(["销售额"]) == []
        assert cache.hasCode("KPI_SALES") is False
        with pytest.raises(RuntimeError, match="未 warmUp"):
            cache.getAll()

    async def test_refreshOne_preserves_the_hit_set(self, dbSession: AsyncSession) -> None:
        """`refreshOne` 后集合不变（该刷新不得让别的 KPI 掉出索引）。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        before = set(_codes(cache.findByAnyKeyword(["销售额", "供应商"])))
        await cache.refreshOne(dbSession, "KPI_SALES")
        after = set(_codes(cache.findByAnyKeyword(["销售额", "供应商"])))
        assert after == before
        assert cache.hasCode("KPI_SALES") is True

    async def test_refreshOne_moves_refreshed_keyword_to_tail(
        self, dbSession: AsyncSession
    ) -> None:
        """**钉死已知怪癖**：`refreshOne` 会把该 KPI 的关键词挪到 `_by_keyword` 插入序末尾。

        `onKpiChanged` 在桶空时 `del` 掉键，`refreshOne` 再用
        `setdefault(kw, []).append(...)` 重建 ⇒ 该关键词排到字典末尾，于是**输出顺序
        相对刷新前会变**（A、B 交叠时从 `[A, B]` 变成 `[B, A]`）。

        这里**刻画现状而非认可**：消费方（`KpiSemanticMatchService` 打分后过滤）不依赖
        相对顺序，故当前无害；但**不得把顺序稳定性当作可依赖的契约** —— 需要稳定顺序
        就得显式排序（那属另开条目）。本断言的作用是：将来谁改了写路径的顺序效应，
        必须在本用例显式确认，而不是悄悄漂走。
        """
        cache = await _warmUp(dbSession, _OVERLAP)
        before = _codes(cache.findByAnyKeyword(["销售额"]))
        # 刷新**插入序最靠前**的那个 KPI：它的关键词桶被删掉后重建，落到末尾
        await cache.refreshOne(dbSession, "KPI_GROWTH")
        after = _codes(cache.findByAnyKeyword(["销售额"]))
        assert before == ["KPI_GROWTH", "KPI_SALES"]
        assert after == ["KPI_SALES", "KPI_GROWTH"], (
            f"refreshOne 的顺序效应变了：before={before!r} after={after!r}"
        )

    async def test_refreshOne_keeps_published_only_invariant(self, dbSession: AsyncSession) -> None:
        """刷新时该 KPI 已转 DRAFT ⇒ 必须从索引移除（不能只增不减）。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        row = (await dbSession.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == "KPI_SALES")
        )).scalar_one()
        row.status = KpiStatus.DRAFT.value
        await dbSession.commit()

        await cache.refreshOne(dbSession, "KPI_SALES")
        assert cache.hasCode("KPI_SALES") is False
        assert _codes(cache.findByAnyKeyword(["销售额"])) == ["KPI_GROWTH"]

    async def test_refreshOne_drops_deleted_kpi(self, dbSession: AsyncSession) -> None:
        """刷新时该 KPI 已被删除 ⇒ 必须从索引移除（删除路径语义）。"""
        cache = await _warmUp(dbSession, _OVERLAP)
        row = (await dbSession.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == "KPI_OTD")
        )).scalar_one()
        await dbSession.delete(row)
        await dbSession.commit()

        await cache.refreshOne(dbSession, "KPI_OTD")
        assert cache.hasCode("KPI_OTD") is False
        assert _codes(cache.findByAnyKeyword(["供应商"])) == []

    async def test_refreshOne_is_noop_before_warm_up(self, dbSession: AsyncSession) -> None:
        """未 warmUp 时 `refreshOne` 直接返回（lifespan 会兜底全量），不写入半份索引。"""
        await _insertKpis(dbSession, _OVERLAP)
        cache = KpiMatchCache()
        await cache.refreshOne(dbSession, "KPI_SALES")
        assert cache.is_warmUp() is False
        assert cache.findByAnyKeyword(["销售额"]) == []
        assert cache.hasCode("KPI_SALES") is False


class TestNotWarmedUp:
    """冷缓存（未 warmUp）的三条读路径必须**静默降级**，而不是抛错打断用户。"""

    async def test_read_paths_degrade_quietly(self) -> None:
        cache = KpiMatchCache()
        assert cache.is_warmUp() is False
        assert cache.findByAnyKeyword(["销售额"]) == []
        assert cache.hasCode("KPI_SALES") is False

    async def test_getAll_raises_because_lifespan_is_a_bug(self) -> None:
        """`getAll` 是唯一**不**降级的读路径：未 warmUp 说明 lifespan 漏了，须显式炸。"""
        cache = KpiMatchCache()
        with pytest.raises(RuntimeError, match="未 warmUp"):
            cache.getAll()
