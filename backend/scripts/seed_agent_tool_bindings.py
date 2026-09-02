"""Idempotent: 把 AGENT_TOOLS dict 中的 binding seed 进 agent_definition.tool_name。

启动时通过 lifespan 调用；DB 已有 binding 的行跳过（手工配置优先）。
仅在 AGENT_TOOLS dict 存在的过渡期使用——commit 6 删 dict 后，本脚本
改从 agent_tool_registry 派生。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AgentDefinition
from app.services.agent_tools import AGENT_TOOLS

logger = logging.getLogger(__name__)


async def seed_agent_tool_bindings(session: AsyncSession) -> int:
    """Seed 所有 tool_name IS NULL 的 Agent 行。

    Returns:
        新增的行数。
    """
    if not AGENT_TOOLS:
        logger.warning("AGENT_TOOLS 为空，跳过 seed")
        return 0

    now = datetime.now(timezone.utc)
    inserted = 0
    for agent_code, tools in AGENT_TOOLS.items():
        tool_name = tools[0]  # 1:1 基数
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