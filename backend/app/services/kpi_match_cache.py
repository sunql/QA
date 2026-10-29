"""Phase 1 Task 1.3: KpiMatchCache — 启动预热 + 写时失效。

两个索引（均只包含 status=PUBLISHED 的 KPI）：
- _by_code：code → KpiCatalog（精确查找）
- _by_keyword：keyword → list[KpiCatalog]（semantic_keywords 子串匹配）

启动时 warmUp() 全量加载；写时通过 SQLAlchemy event listener 触发
onKpiChanged() 失效。

单实例部署；写入路径（KpiCatalogService）调用 onKpiChanged 清空缓存条目。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper

from app.domain.enums import KpiStatus

if TYPE_CHECKING:
    from app.domain.models import KpiCatalog

logger = logging.getLogger(__name__)

# 缓存 TTL（秒）：超过则读路径重建，兜住绕过 ORM listener 的写入
_CACHE_MAX_AGE_SECONDS = 60


def _hasPendingKpiChanges(session: AsyncSession) -> bool:
    """session 里是否有**待落库**的 KpiCatalog 改动。

    `populate_existing=True` 会用查询结果**覆盖** identity map 中同 id 实例的
    属性 ⇒ 若调用方改了 KpiCatalog 却还没 flush，这次 SELECT 会把改动抹掉
    （生产 session `autoflush=False` + `expire_on_commit=False`
    ⇒ 后续 flush 会把**旧值**写回 DB = 静默数据丢失）。

    ⇒ 有待落库改动时退回普通 SELECT：可能读到略旧的值（缓存是加速器，
    TTL 到期后自愈），但绝不毁数据。

    注：`session.new` / `session.dirty` / `session.deleted` 的读取**不触发
    autoflush**（它们只扫 identity map 与属性历史），因此这里做检测是安全的。
    """
    from app.domain.models import KpiCatalog

    pending = (*session.new, *session.dirty, *session.deleted)
    return any(isinstance(obj, KpiCatalog) for obj in pending)


class KpiMatchCache:
    """L1 语义匹配缓存。

    两个只读索引（由 refresh() 全量重建）：
    - _by_code：kpi_code → KpiCatalog
    - _by_keyword：keyword 子串 → list[KpiCatalog]

    写入时 onKpiChanged(kpi_code, kpiId=…) 失效对应条目，下次查询时触发 lazy rebuild。

    ⚠️ **失效按 id 定位**（id 跨改名稳定）：只按 code 失效会漏掉改名后的旧 code
    残留（C1）——命中了但口径是旧的。
    ⚠️ **冷缓存不 no-op**：冷态下 `refreshOne` 改为全量重建（C2）——否则缓存
    永久停在冷态，`findByAnyKeyword` 恒空 ⇒ 语义匹配静默失效。
    ⚠️ **TTL 兜底**：读路径 `ensureFresh` 按 TTL 重建，兜住绕过 ORM listener 的
    写入（Core UPDATE / 裸 SQL）（C3）。

    ⚠️ **未来扩展示例**（`chat-service-assessment.md` §15 末尾第 1 项登记，
    当前未启用）：如果目录规模真的上来（千级 KPI / 万级关键词），正确结构是
    **Aho-Corasick / 后缀自动机**（多模式子串匹配的线性时间算法）而不是
    子串枚举 —— 现行 `_by_keyword` 字典查找的复杂度是 O(keywords × catalog_keywords)，
    在千级场景下会从亚毫秒退化到毫秒级。换索引时必须与 `_by_keyword` **写路径同生命周期**
    （`onKpiChanged` / `refreshOne` 必须重建自动机），否则会复现倒排索引那次的
    静默偏离（写路径漏改、读路径命中陈旧数据）。**准入压测**（500 关键词 × 30 字
    字典 + 倒排子串索引实测 +42.3 MB 纯开销）是当初否决该方案的真实数据，禁止
    凭直觉「越大越需要索引」—— 实测收益在当前规模下为零。当前实现保留作为 SSOT。
    """

    def __init__(self) -> None:
        self._by_code: dict[str, "KpiCatalog"] = {}
        self._by_keyword: dict[str, list["KpiCatalog"]] = {}
        self._loaded: bool = False
        # 脏标记：任何失效后置 True，读路径 ensureFresh 见到就重建。
        self._dirty: bool = False
        # 上次全量加载时刻（time.monotonic 基准），TTL 兜底 raw SQL 绕过。
        self._loadedAt: float = 0.0

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def getAll(self) -> list["KpiCatalog"]:
        """返回缓存中所有 active KPI（不可变视图副本）。"""
        if not self._loaded:
            raise RuntimeError("KpiMatchCache 未 warmUp（lifespan bug）")
        return list(self._by_code.values())

    def hasCode(self, code: str) -> bool:
        """code 是否存在于缓存中。

        未 warmUp 时返回 False（降级到 LLM 流水线，不阻断用户）。
        """
        if not self._loaded:
            return False
        return code in self._by_code

    def is_warmUp(self) -> bool:
        """缓存是否已 warmUp。"""
        return self._loaded

    def findByAnyKeyword(self, keywords: list[str]) -> list["KpiCatalog"]:
        """返回 semantic_keywords 包含任意一个**非空** keyword 的 KPI 列表。

        匹配方式：keyword 是 catalog keyword 的子串（大小写不敏感）。
        返回去重列表（一个 KPI 可能被多个 keyword 命中，只出现一次）。
        未 warmUp 时返回空列表（降级到 LLM 流水线，不阻断用户）。

        **空串关键词显式跳过**（2026-09-27 第五批，`chat-service-assessment.md` §15 末尾
        第 2 项）：`"" in s` 在 substring 语义里恒真，会命中全部 KPI —— 这是副作用
        而非设计意图（合法调用路径 `_extractKeywords` 仅产出 2+ 字 ngram + 调用前
        `if user_kws:` 守卫，不会产生 `[""]`）。本批判定为缺陷并修复：「空 needle
        在 substring 搜索里应是 no-op」，返回**空集合**而非全量。详见
        `test_kpi_match_cache.py::test_empty_string_keyword_is_skipped`。
        """
        if not self._loaded:
            return []
        seen: set[int] = set()
        result: list["KpiCatalog"] = []
        for kw in keywords:
            if not kw:
                continue  # 空串跳过（防御：空 needle 不应触发「匹配全部」副作用）
            kw_lower = kw.lower()
            for cat_kw, kpis in self._by_keyword.items():
                if kw_lower in cat_kw.lower():
                    for kpi in kpis:
                        if id(kpi) not in seen:
                            seen.add(id(kpi))
                            result.append(kpi)
        return result

    async def warmUp(self, session: AsyncSession) -> None:
        """全量加载 status=PUBLISHED 的 KPI，重建两个索引。

        `populate_existing=True`：**强制用 DB 当前值刷新**已在 identity map 里的
        同 id 行。默认行为下 SELECT 会直接返回 identity map 里的旧实例（属性不
        刷新）⇒ 绕过 ORM 的写入（Core UPDATE / 裸 SQL）即使重建了索引，读到的
        仍是旧口径。缓存必须反映 DB 现状，故显式声明刷新语义。

        ⚠️ **契约**：该刷新语义会**覆盖调用方 session 里同 id 行的内存属性**。
        若调用方 session 中存在**待落库**的 KpiCatalog 改动（改了但没 flush），
        这次刷新会把改动静默抹掉（生产 session `autoflush=False` ⇒ 随后 flush
        把旧值写回 DB = 数据丢失）。因此这里**条件化**：检测到待落库改动时退回
        普通 SELECT（读到略旧的值，但绝不毁数据；缓存是加速器，TTL 到期自愈）。
        """
        from app.domain.models import KpiCatalog

        stmt = select(KpiCatalog).where(KpiCatalog.status == KpiStatus.PUBLISHED.value)
        if not _hasPendingKpiChanges(session):
            stmt = stmt.execution_options(populate_existing=True)
        rows = await session.execute(stmt)
        kpis = list(rows.scalars().all())

        by_code: dict[str, "KpiCatalog"] = {}
        by_keyword: dict[str, list["KpiCatalog"]] = {}

        for kpi in kpis:
            if kpi.kpi_code:
                by_code[kpi.kpi_code] = kpi

            kws = kpi.semantic_keywords
            if kws:
                for kw in kws:
                    if kw not in by_keyword:
                        by_keyword[kw] = []
                    by_keyword[kw].append(kpi)

        self._by_code = by_code
        self._by_keyword = by_keyword
        self._loaded = True
        self._dirty = False
        self._loadedAt = time.monotonic()
        logger.info("KpiMatchCache warmed up: %d active KPIs, %d keywords", len(by_code), len(by_keyword))

    def onKpiChanged(self, kpi_code: str | None = None, *, kpiId: int | None = None) -> None:
        """写时失效：清空指定条目或全量。

        失效**按 id 定位**（id 跨改名稳定）：改名场景下按 code 失效会漏掉旧 code
        的残留条目（C1）。传 kpiId 时按 id 从两个索引中移除该 KPI 的**所有**引用。

        写入路径（KpiCatalogService.create/update/delete）在事务提交后调用此方法。
        """
        if kpi_code is None and kpiId is None:
            # 全量清空（新建容器替换，不原地 clear —— 保持不可变语义）
            self._by_code = {}
            self._by_keyword = {}
            self._loaded = False
            self._dirty = True
            logger.debug("KpiMatchCache 全量失效")
            return
        self._removeEntries(kpi_code=kpi_code, kpiId=kpiId)
        self._dirty = True
        logger.debug("KpiMatchCache 失效: code=%s id=%s", kpi_code, kpiId)

    def _removeEntries(self, *, kpi_code: str | None, kpiId: int | None) -> None:
        """从两个索引中移除匹配条目（copy-on-write：新建容器后整体替换）。

        匹配条件（任一命中即移除）：`_by_code` 的 key 等于 kpi_code，或条目 id
        等于 kpiId。关键词索引按**条目的 id** 与 kpi_code 一并摘除 —— 改名场景下
        缓存里的旧条目 key 是旧 code，但行 id 不变 ⇒ 按 id 摘才摘得掉（C1）。
        空关键词桶整体丢弃（不留空 list）。
        """
        byCode: dict[str, KpiCatalog] = {}
        for code, kpi in self._by_code.items():
            if code == kpi_code or (kpiId is not None and kpi.id == kpiId):
                continue
            byCode[code] = kpi
        byKeyword: dict[str, list[KpiCatalog]] = {}
        for kw, kpis in self._by_keyword.items():
            kept = [
                kpi
                for kpi in kpis
                if kpi.kpi_code != kpi_code
                and not (kpiId is not None and kpi.id == kpiId)
            ]
            if kept:
                byKeyword[kw] = kept
        self._by_code = byCode
        self._by_keyword = byKeyword

    async def refreshOne(
        self, session: AsyncSession, kpi_code: str, *, kpiId: int | None = None
    ) -> None:
        """写时刷新单条（update 路径）：重新查询该 KPI 并更新索引。

        **冷缓存不再 no-op**（C2）：缓存停在冷态时 `findByAnyKeyword` 恒返回空
        ⇒ 语义匹配静默失效 ⇒ Tier B 契约永远注不进去。此时改为全量重建。

        kpiId：用于按 id 失效（改名场景，见 onKpiChanged）。
        """
        if not self._loaded:
            logger.info(
                "KpiMatchCache 冷缓存，refreshOne 改为全量重建 code=%s", kpi_code
            )
            await self.warmUp(session)
            return
        from app.domain.models import KpiCatalog

        # 先清理该 KPI 在两个索引中的旧条目（按 id + 按 code 双保险）
        self.onKpiChanged(kpi_code, kpiId=kpiId)

        row = await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == kpi_code)
        )
        kpi = row.scalar_one_or_none()

        if kpi is None:
            # 该 KPI 已被删除或不存在
            return

        # 与 warmUp 同口径：仅 status=PUBLISHED 入索引。曾误引 kpi.is_enabled
        # （85907cb）——kpi_catalog 无此列，读即 AttributeError。
        if kpi.status == KpiStatus.PUBLISHED.value:
            byCode = {**self._by_code, kpi_code: kpi}
            byKeyword: dict[str, list[KpiCatalog]] = {
                kw: list(kpis) for kw, kpis in self._by_keyword.items()
            }
            for kw in (kpi.semantic_keywords or []):
                byKeyword[kw] = [*byKeyword.get(kw, []), kpi]
            self._by_code = byCode
            self._by_keyword = byKeyword
        self._dirty = False

    async def ensureFresh(
        self, session: AsyncSession, *, maxAgeSeconds: int = _CACHE_MAX_AGE_SECONDS
    ) -> None:
        """确保缓存处于可用且不过期的状态（**读路径**调用）。

        触发全量重建的三种情况：

        1. 冷缓存（从未 warmUp）—— 否则 `getAll()` 抛 RuntimeError、
           `findByAnyKeyword` 恒空 ⇒ 语义匹配静默失效
        2. 脏标记（写路径失效过，尚未定向刷新）
        3. 超过 maxAgeSeconds —— **兜住绕过 ORM listener 的写入**
           （Core UPDATE / 裸 SQL / 手工改库），C3 的最后一道防线

        缓存新鲜时**不查库**（no-op）。
        """
        if not self._loaded or self._dirty:
            await self.warmUp(session)
            return
        if (time.monotonic() - self._loadedAt) > maxAgeSeconds:
            await self.warmUp(session)


# ---------------------------------------------------------------------------
# SQLAlchemy event listener — 监听 kpi_catalog 表的 DML，触发缓存失效
# ---------------------------------------------------------------------------

def _setup_kpi_catalog_listeners() -> None:
    """在 Mapper 级别注册 insert/update/delete 监听，触发缓存失效。

    注册一次（module load 时）；写入路径通过 KpiCatalogService 调用
    onKpiChanged(kpi_code, kpiId=…) 主动失效。
    """

    def _invalidate(target: KpiCatalog) -> None:
        """ORM 写入后失效缓存条目。

        listener 是**同步**回调（跑在事件循环线程的 greenlet 里），只做 dict 变更
        （无 await）⇒ 无跨线程风险。失效发生在**提交前**，方向保守：宁可缓存偏冷，
        不可缓存偏陈旧；提交后由 KpiCatalogService.refreshOne 重新写入。

        ⚠️ Core UPDATE（`sqlalchemy.update`）**不触发** mapper 事件 ⇒ 绕过本
        listener。这类写入由读路径 ensureFresh 的 TTL 兜底（见 ensureFresh）。
        """
        try:
            get_kpi_match_cache().onKpiChanged(
                target.kpi_code, kpiId=target.id
            )
        except Exception:
            logger.warning(
                "KPI 缓存失效失败 code=%s", target.kpi_code, exc_info=True
            )

    @event.listens_for(KpiCatalog, "after_insert")
    def _after_insert(mapper: Mapper, connection: Any, target: "KpiCatalog") -> None:
        _invalidate(target)

    @event.listens_for(KpiCatalog, "after_update")
    def _after_update(mapper: Mapper, connection: Any, target: "KpiCatalog") -> None:
        _invalidate(target)

    @event.listens_for(KpiCatalog, "after_delete")
    def _after_delete(mapper: Mapper, connection: Any, target: "KpiCatalog") -> None:
        _invalidate(target)


# 模块加载时注册监听（一次性）
from app.domain.models import KpiCatalog  # noqa: E402
_setup_kpi_catalog_listeners()


# 模块级单例（供 FastAPI lifespan 使用）
kpi_match_cache = KpiMatchCache()


def get_kpi_match_cache() -> KpiMatchCache:
    """返回模块级单例（供 KpiCatalogService 等调用写时失效）。"""
    return kpi_match_cache
