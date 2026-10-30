"""数据域注册表（feat-data-domain-layer 方案 1）。

businessObjectRegistry 同款模式，但带 **enabled 语义**：
- warmUp：bulk-load 全部行，缓存 (allCodes, enabledCodes)
- isValid(code)：仅 enabled 码通过——禁用域不可用于新写入（存量行不动）；
  删除域由 FK RESTRICT 兜底
- 未 warmed → RuntimeError（同 businessObjectRegistry fail-fast；Agent 授权域
  校验方在 schemas._vocabCheckDomains，另有常量 fallback，见该处说明）
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataDomain

logger = logging.getLogger(__name__)


class DataDomainRegistry:
    def __init__(self) -> None:
        self._allCodes: frozenset[str] = frozenset()
        self._enabledCodes: frozenset[str] = frozenset()
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        rows = (
            await session.execute(select(DataDomain.code, DataDomain.enabled))
        ).all()
        async with self._lock:
            self._allCodes = frozenset(r[0] for r in rows)
            self._enabledCodes = frozenset(r[0] for r in rows if r[1])
            self._loaded = True
        logger.info(
            "DataDomainRegistry warmed up: %d domains (%d enabled)",
            len(self._allCodes), len(self._enabledCodes),
        )

    def isValid(self, code: str) -> bool:
        """仅 enabled 码合法（新写入边界）；未 warmed → RuntimeError。"""
        if not self._loaded:
            raise RuntimeError("DataDomainRegistry 未 warmUp（lifespan bug）")
        return code in self._enabledCodes

    def isWarmedUp(self) -> bool:
        return self._loaded

    def invalidate(self) -> None:
        """写时失效（同步快路径，不查 DB）。未 warmed 时为 no-op。"""
        if not self._loaded:
            return
        self._allCodes = frozenset()
        self._enabledCodes = frozenset()
        self._loaded = False

    def getEnabled(self) -> frozenset[str]:
        """仅 enabled 码（词表/授权校验用）；未 warmed → RuntimeError。"""
        if not self._loaded:
            raise RuntimeError("DataDomainRegistry 未 warmUp（lifespan bug）")
        return self._enabledCodes

    def getAll(self) -> frozenset[str]:
        if not self._loaded:
            raise RuntimeError("DataDomainRegistry 未 warmUp（lifespan bug）")
        return self._allCodes

    async def reloadOne(self, session: AsyncSession, code: str) -> None:
        async with self._lock:
            row = (
                await session.execute(
                    select(DataDomain.code, DataDomain.enabled).where(
                        DataDomain.code == code
                    )
                )
            ).first()
            if row is None:
                self._allCodes = self._allCodes - {code}
                self._enabledCodes = self._enabledCodes - {code}
                return
            self._allCodes = self._allCodes | {row[0]}
            if row[1]:
                self._enabledCodes = self._enabledCodes | {row[0]}
            else:
                self._enabledCodes = self._enabledCodes - {row[0]}


# 模块级单例：lifespan warmUp；Pydantic validator 共享。
dataDomainRegistry = DataDomainRegistry()
