"""数据层规则注册表（feat-data-domain-layer 方案 2）。

businessObjectRegistry 同款模式：
- warmUp(session)：lifespan 调用，bulk-load 全部 enabled=True 行
- rules()：sync 快路径，按 priority 升序；未 warmed → DEFAULT_LAYER_RULES fallback
  （schema validator 在 Pydantic 解析期调用，fallback 而非 RuntimeError，
  保证未 warmUp 的单测可用且授权集合仍闭合）
- distinctLayers()：Agent 层词表（含手工声明层）
- reloadAll(session)：写后重载（本期规则无管理端 CRUD，预留）
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataLayerRule
from app.services.data_layer_defaults import DEFAULT_LAYER_RULES, LayerRule

logger = logging.getLogger(__name__)


def _toRule(row: DataLayerRule) -> LayerRule:
    return LayerRule(layer=row.layer, pattern=row.pattern, priority=row.priority)


class LayerRuleRegistry:
    def __init__(self) -> None:
        self._rules: tuple[LayerRule, ...] = DEFAULT_LAYER_RULES
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        rows = (
            await session.execute(
                select(DataLayerRule).where(DataLayerRule.enabled.is_(True))
            )
        ).scalars().all()
        async with self._lock:
            self._rules = tuple(sorted((_toRule(r) for r in rows), key=lambda r: r.priority))
            self._loaded = True
        logger.info("LayerRuleRegistry warmed up: %d rules", len(self._rules))

    def isWarmedUp(self) -> bool:
        return self._loaded

    def invalidate(self) -> None:
        """写时失效（同步快路径，不查 DB）；回退到默认规则并清 loaded 标记。"""
        if not self._loaded:
            return
        self._rules = DEFAULT_LAYER_RULES
        self._loaded = False

    def rules(self) -> tuple[LayerRule, ...]:
        """按 priority 升序；未 warmed → 默认规则（闭合 fallback）。"""
        return self._rules

    def distinctLayers(self) -> tuple[str, ...]:
        """层词表 = 全部 enabled 规则的 layer 去重（含 pattern=NULL 手工层）。"""
        seen: dict[str, None] = {}
        for rule in self._rules:
            seen.setdefault(rule.layer, None)
        return tuple(seen)

    async def reloadAll(self, session: AsyncSession) -> None:
        await self.warmUp(session)


# 模块级单例：lifespan warmUp；schema validator / chat_recall 共享。
layerRuleRegistry = LayerRuleRegistry()
