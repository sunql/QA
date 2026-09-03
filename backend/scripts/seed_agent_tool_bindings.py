"""Idempotent: 把 _AGENT_DEFAULT_BINDINGS 常量中的 binding seed 进 agent_definition.tool_name.

启动时通过 lifespan 调用；DB 已有 binding 的行跳过（手工配置优先）。

T8 refactor：源 binding dict 从 agent_tools（已删除 AGENT_DEFAULT_BINDINGS）
迁至 scripts.seed_agents._AGENT_DEFAULT_BINDINGS（seed 元数据 SSOT）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentDefinition
from scripts.seed_agents import _AGENT_DEFAULT_BINDINGS

logger = logging.getLogger(__name__)


async def seed_agent_tool_bindings(session: AsyncSession) -> int:
    """Seed 所有 tool_name IS NULL 的 Agent 行。

    Returns:
        新增的行数。
    """
    if not _AGENT_DEFAULT_BINDINGS:
        logger.warning("_AGENT_DEFAULT_BINDINGS 为空，跳过 seed")
        return 0

    now = datetime.now(timezone.utc)
    inserted = 0
    for agent_code, tool_name in _AGENT_DEFAULT_BINDINGS.items():
        result = await session.execute(
            update(AgentDefinition)
            .where(AgentDefinition.agent_code == agent_code)
            .where(AgentDefinition.tool_name.is_(None))
            .values(tool_name=tool_name, tool_name_updated_at=now)
            .returning(AgentDefinition.id)
        )
        if result.scalar_one_or_none() is not None:
            inserted += 1
            logger.info("seeded binding: %s → %s", agent_code, tool_name)
    await session.commit()
    return inserted
