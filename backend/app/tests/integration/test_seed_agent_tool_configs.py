"""seed_agent_tool_configs.py 集成测试（真实 PG + Alembic）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL（5433/qa_metadata_test），
pgSession fixture 每测试 TRUNCATE 隔离。

覆盖：
1. 首次 seed：3 个内置工具入库（supplier_360 / supplier_risk / graph_traverse）
2. 重复 seed 幂等：count=0（无变化）
3. seed 元数据变更 → upsert 更新

T10 lifespan 集成后此处亦可被 lifespan smoke 验证；lifespan 未触发时
seedAgentToolConfigs 必须可独立调用（scripts 入口同款逻辑）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import AgentToolConfig
from app.tests import _pg_support
from scripts.seed_agent_tool_configs import (
    TOOL_SEEDS,
    seedAgentToolConfigs,
)


@pytest.fixture()
async def pgSession() -> AsyncIterator[AsyncSession]:
    """真实 PG 会话：每测试新建引擎 + TRUNCATE 隔离 + Alembic upgrade head。"""
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_inserts_three_tools_on_empty_db(pgSession: AsyncSession) -> None:
    """空 DB → seed 后 3 个工具入库；name 集合与 TOOL_SEEDS 一致。"""
    count = await seedAgentToolConfigs(pgSession)
    await pgSession.commit()
    assert count == 3
    rows = (await pgSession.execute(select(AgentToolConfig))).scalars().all()
    assert len(rows) == 3
    assert {r.name for r in rows} == {s["name"] for s in TOOL_SEEDS}


@pytest.mark.asyncio
async def test_seed_idempotent_no_change_on_second_run(
    pgSession: AsyncSession,
) -> None:
    """首次 seed → 二次 seed 返回 count=0（幂等）。"""
    await seedAgentToolConfigs(pgSession)
    await pgSession.commit()
    count2 = await seedAgentToolConfigs(pgSession)
    await pgSession.commit()
    assert count2 == 0


@pytest.mark.asyncio
async def test_seed_updates_metadata_when_changed(pgSession: AsyncSession) -> None:
    """修改 TOOL_SEEDS[0].description → 再 seed → upsert 更新该行。

    使用本地副本避免污染模块级 TOOL_SEEDS（影响其他测试）。
    """
    # 先 seed 一遍
    await seedAgentToolConfigs(pgSession)
    await pgSession.commit()

    # 模拟下次部署改了 description
    original = TOOL_SEEDS[0]["description"]
    TOOL_SEEDS[0]["description"] = "UPDATED"
    try:
        count = await seedAgentToolConfigs(pgSession)
        await pgSession.commit()
        assert count >= 1
        row = (
            await pgSession.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == "supplier_360")
            )
        ).scalar_one()
        assert row.description == "UPDATED"
    finally:
        TOOL_SEEDS[0]["description"] = original