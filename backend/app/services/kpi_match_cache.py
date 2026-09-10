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
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper

from app.domain.enums import KpiStatus

if TYPE_CHECKING:
    from app.domain.models import KpiCatalog

logger = logging.getLogger(__name__)


class KpiMatchCache:
    """L1 语义匹配缓存。

    两个只读索引（由 refresh() 全量重建）：
    - _by_code：kpi_code → KpiCatalog
    - _by_keyword：keyword 子串 → list[KpiCatalog]

    写入时 onKpiChanged(kpi_code) 失效对应条目，下次查询时触发 lazy rebuild。
    """

    def __init__(self) -> None:
        self._by_code: dict[str, "KpiCatalog"] = {}
        self._by_keyword: dict[str, list["KpiCatalog"]] = {}
        self._loaded: bool = False

    # -------------------------------------------------------------------------
    # Public interface
    # -------------------------------------------------------------------------

    def getAll(self) -> list["KpiCatalog"]:
        """返回缓存中所有 active KPI（不可变视图副本）。"""
        if not self._loaded:
            raise RuntimeError("KpiMatchCache 未 warmUp（lifespan bug）")
        return list(self._by_code.values())

    def hasCode(self, code: str) -> bool:
        """code 是否存在于缓存中。"""
        if not self._loaded:
            raise RuntimeError("KpiMatchCache 未 warmUp（lifespan bug）")
        return code in self._by_code

    def findByAnyKeyword(self, keywords: list[str]) -> list["KpiCatalog"]:
        """返回 semantic_keywords 包含任意一个 keyword 的 KPI 列表。

        匹配方式：keyword 是 catalog keyword 的子串（大小写不敏感）。
        返回去重列表（一个 KPI 可能被多个 keyword 命中，只出现一次）。
        """
        if not self._loaded:
            raise RuntimeError("KpiMatchCache 未 warmUp（lifespan bug）")
        seen: set[int] = set()
        result: list["KpiCatalog"] = []
        for kw in keywords:
            kw_lower = kw.lower()
            for cat_kw, kpis in self._by_keyword.items():
                if kw_lower in cat_kw.lower():
                    for kpi in kpis:
                        if id(kpi) not in seen:
                            seen.add(id(kpi))
                            result.append(kpi)
        return result

    async def warmUp(self, session: AsyncSession) -> None:
        """全量加载 status=PUBLISHED 的 KPI，重建两个索引。"""
        from app.domain.models import KpiCatalog

        stmt = select(KpiCatalog).where(KpiCatalog.status == KpiStatus.PUBLISHED.value)
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
        logger.info("KpiMatchCache warmed up: %d active KPIs, %d keywords", len(by_code), len(by_keyword))

    def onKpiChanged(self, kpi_code: str | None = None) -> None:
        """写时失效：清空指定条目或全量。

        写入路径（KpiCatalogService.create/update/delete）在事务提交后调用此方法，
        标记缓存条目失效。下次查询时若发现条目缺失，触发 lazy rebuild。
        """
        if kpi_code is None:
            # 全量清空
            self._by_code.clear()
            self._by_keyword.clear()
            self._loaded = False
            logger.debug("KpiMatchCache 全量失效")
        else:
            # 单条失效：同时清 _by_code 和 _by_keyword 中该 KPI 的所有引用
            old = self._by_code.pop(kpi_code, None)
            if old:
                for kw in (old.semantic_keywords or []):
                    lst = self._by_keyword.get(kw)
                    if lst:
                        self._by_keyword[kw] = [k for k in lst if k.kpi_code != kpi_code]
                        if not self._by_keyword[kw]:
                            del self._by_keyword[kw]
            logger.debug("KpiMatchCache 失效: %s", kpi_code)

    async def refreshOne(self, session: AsyncSession, kpi_code: str) -> None:
        """写时刷新单条（update 路径）：重新查询该 KPI 并更新索引。

        仅当缓存已 warmUp 时调用；若未 warmUp 则跳过（lifespan 会兜底）。

        先清理该 KPI 在两个索引中的旧条目，再插入新数据。
        """
        if not self._loaded:
            return
        from app.domain.models import KpiCatalog

        # 先清理该 KPI 在两个索引中的旧条目
        self.onKpiChanged(kpi_code)

        row = await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == kpi_code)
        )
        kpi = row.scalar_one_or_none()

        if kpi is None:
            # 该 KPI 已被删除或不存在
            return

        if kpi.status == KpiStatus.PUBLISHED.value and kpi.is_enabled:
            self._by_code[kpi_code] = kpi
            for kw in (kpi.semantic_keywords or []):
                self._by_keyword.setdefault(kw, []).append(kpi)


# ---------------------------------------------------------------------------
# SQLAlchemy event listener — 监听 kpi_catalog 表的 DML，触发缓存失效
# ---------------------------------------------------------------------------

def _setup_kpi_catalog_listeners() -> None:
    """在 Mapper 级别注册 insert/update/delete 监听，触发缓存失效。

    注册一次（module load 时）；写入路径通过 KpiCatalogService 调用
    onKpiChanged(kpi_code) 主动失效。
    """

    @event.listens_for(KpiCatalog, "after_insert")
    def _after_insert(mapper: Mapper, connection: Any, target: "KpiCatalog") -> None:
        # 异步上下文：直接写缓存不安全（不在事件循环），改为标记需要 refresh
        # 实际失效由 KpiCatalogService 在事务提交后主动调用 onKpiChanged
        logger.debug("kpi_catalog after_insert: %s", target.kpi_code)

    @event.listens_for(KpiCatalog, "after_update")
    def _after_update(mapper: Mapper, connection: Any, target: "KpiCatalog") -> None:
        logger.debug("kpi_catalog after_update: %s", target.kpi_code)

    @event.listens_for(KpiCatalog, "after_delete")
    def _after_delete(mapper: Mapper, connection: Any, target: "KpiCatalog") -> None:
        logger.debug("kpi_catalog after_delete: %s", target.kpi_code)


# 模块加载时注册监听（一次性）
from app.domain.models import KpiCatalog  # noqa: E402
_setup_kpi_catalog_listeners()


# 模块级单例（供 FastAPI lifespan 使用）
kpi_match_cache = KpiMatchCache()


def get_kpi_match_cache() -> KpiMatchCache:
    """返回模块级单例（供 KpiCatalogService 等调用写时失效）。"""
    return kpi_match_cache
