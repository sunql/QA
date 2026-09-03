"""DB-backed 工具注册表（替代硬编码 agent_tool_registry，feat-agent-tool-config-db）。

- warmUp：lifespan 调用，bulk-load 所有 enabled=True 行并装配为 AgentTool
- get(name)：sync 快路径；未 warmed → RuntimeError（lifespan bug）
- has(name) / all()：同上的 sync fast path
- invalidate(name|name=None)：写时失效（sync，不查 DB）；下次 _resolveTool 触发 reload_one
- reload_one(session, name)：async，asyncio.Lock 防并发 reload_one 竞态
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentToolConfig
from app.services.agent_tools import AgentTool, AgentToolAssembly

logger = logging.getLogger(__name__)


class AgentToolConfigRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        """lifespan 启动时调用：加载 enabled=True 的所有行并装配。"""
        rows = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.enabled.is_(True))
            )
        ).scalars().all()
        async with self._lock:
            self._tools = {
                row.name: AgentToolAssembly.assemble(row) for row in rows
            }
            self._loaded = True
        logger.info("AgentToolConfigRegistry warmed up: %d tools", len(self._tools))

    def get(self, name: str) -> AgentTool | None:
        if not self._loaded:
            raise RuntimeError("AgentToolConfigRegistry 未 warmUp（lifespan bug）")
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        if not self._loaded:
            raise RuntimeError("AgentToolConfigRegistry 未 warmUp（lifespan bug）")
        return name in self._tools

    def all(self) -> list[AgentTool]:
        if not self._loaded:
            raise RuntimeError("AgentToolConfigRegistry 未 warmUp（lifespan bug）")
        return [self._tools[k] for k in sorted(self._tools.keys())]

    def invalidate(self, name: str | None = None) -> None:
        """写时失效（同步快路径，不查 DB）。未 warmed 时为 no-op（防 lifespan bug 雪崩）。"""
        if not self._loaded:
            return
        if name is None:
            self._tools.clear()
        else:
            self._tools.pop(name, None)

    async def reload_one(self, session: AsyncSession, name: str) -> None:
        """Cache miss 时单行重载；DB 行不存在或被禁用 → 从 cache 移除。

        asyncio.Lock 防并发 reload_one 竞态：若两个请求同时触发 reload_one，
        第二个会等第一个完成后再次执行 SELECT —— 仍是幂等，且保证最终一致。
        """
        async with self._lock:
            row = (
                await session.execute(
                    select(AgentToolConfig).where(AgentToolConfig.name == name)
                )
            ).scalar_one_or_none()
            if row is None or not row.enabled:
                self._tools.pop(name, None)
                return
            self._tools[name] = AgentToolAssembly.assemble(row)


# 模块级单例：lifespan warmUp；AgentRuntimeService._resolveTool 共享。
agent_tool_config_registry = AgentToolConfigRegistry()
