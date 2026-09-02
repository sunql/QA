"""Agent → Tool 绑定缓存：启动预热 + 写时失效。

单实例部署；未来多实例切换 Redis（独立 change）。
"""
from __future__ import annotations

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentDefinition
from app.domain.enums import AgentStatus

logger = logging.getLogger(__name__)


class AgentBindingCache:
    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}
        self._loaded: bool = False

    async def warmUp(self, session: AsyncSession) -> None:
        rows = await session.execute(
            select(AgentDefinition.agent_code, AgentDefinition.tool_name)
            .where(AgentDefinition.tool_name.is_not(None))
            .where(AgentDefinition.status == AgentStatus.ACTIVE.value)
        )
        self._cache = {code: tool for code, tool in rows.all()}
        self._loaded = True
        logger.info("AgentBindingCache warmed up: %d active bindings", len(self._cache))

    def getToolName(self, agent_code: str) -> str | None:
        if not self._loaded:
            raise RuntimeError("AgentBindingCache 未 warmUp（lifespan bug）")
        return self._cache.get(agent_code)

    def invalidate(self, agent_code: str | None = None) -> None:
        if agent_code is None:
            self._cache.clear()
        else:
            self._cache.pop(agent_code, None)

    async def refreshOne(self, session: AsyncSession, agent_code: str) -> None:
        if not self._loaded:
            return  # 写前若未 warmUp，跳过（lifespan 会兜底）
        row = await session.execute(
            select(AgentDefinition.tool_name)
            .where(AgentDefinition.agent_code == agent_code)
        )
        self._cache[agent_code] = row.scalar_one_or_none()


agent_binding_cache = AgentBindingCache()  # 模块级单例