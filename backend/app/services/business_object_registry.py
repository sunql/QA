"""DB-backed 业务对象注册表（feat-business-object-dynamic）。

- warmUp: lifespan 调用，bulk-load 所有 business_object.code
- isValid(code): sync 快路径；未 warmed → RuntimeError
- getAll(): sync，返回缓存的 codes frozenset
- reloadOne(session, code): async，asyncio.Lock 防并发 reloadOne 竞态
- invalidate(): sync，清空缓存（for full reload scenarios）
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import BusinessObject

logger = logging.getLogger(__name__)


class BusinessObjectRegistry:
    def __init__(self) -> None:
        self._codes: frozenset[str] = frozenset()
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        """lifespan 启动时调用：加载所有 business_object.code。"""
        rows = (
            await session.execute(select(BusinessObject.code))
        ).scalars().all()
        async with self._lock:
            self._codes = frozenset(rows)
            self._loaded = True
        logger.info("BusinessObjectRegistry warmed up: %d codes", len(self._codes))

    def isValid(self, code: str) -> bool:
        """Sync 快路径；未 warmed → RuntimeError（lifespan bug）。"""
        if not self._loaded:
            raise RuntimeError("BusinessObjectRegistry 未 warmUp（lifespan bug）")
        return code in self._codes

    def getAll(self) -> frozenset[str]:
        """Sync，返回缓存的 codes。"""
        if not self._loaded:
            raise RuntimeError("BusinessObjectRegistry 未 warmUp（lifespan bug）")
        return self._codes

    async def reloadOne(self, session: AsyncSession, code: str) -> None:
        """Cache miss 时单行重载；DB 行不存在 → 从 cache 移除。

        asyncio.Lock 防并发 reloadOne 竞态。
        """
        async with self._lock:
            row = (
                await session.execute(
                    select(BusinessObject.code).where(BusinessObject.code == code)
                )
            ).scalar_one_or_none()
            if row is None:
                # Code was deleted — remove from cache
                self._codes = self._codes - {code}
                return
            # Code exists — add to cache
            self._codes = self._codes | {row}

    def invalidate(self) -> None:
        """写时失效（同步快路径，不查 DB）。未 warmed 时为 no-op。"""
        if not self._loaded:
            return
        self._codes = frozenset()
        self._loaded = False


# 模块级单例：lifespan warmUp；Pydantic validator 共享。
businessObjectRegistry = BusinessObjectRegistry()
