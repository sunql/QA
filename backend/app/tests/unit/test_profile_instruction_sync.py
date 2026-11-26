"""存量 profile 指令同步（2026-11-26）。

**问题**：`seed_procurement_agent_profiles` 的既有约定是「profile 已存在 ⇒
不覆盖 instructions」（管理员可能改过）⇒ 改了 `_READONLY_BASE` 措辞之后，
**存量部署的指令永远停在旧版** ⇒ LLM 继续把工具名印进正文。

**解法**：只在存量指令**逐字等于上一版种子**时替换成新版（那是「把改种子落下去」，
不是「抢改管理员的字段」）。管理员改过的指令与上一版不等 ⇒ 一律不动。

本用例钉住三种情形：旧种子→刷新、已最新→不动、管理员改过→不动。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.agent_orchestration_models import AgentOrchestrationProfile
from app.domain.models import AgentDefinition
from scripts.seed_procurement_agent_profiles import (
    _LEGACY_READONLY_BASE,
    _READONLY_BASE,
    PROFILE_SEEDS,
    _legacyInstructionsFor,
)

_ACTOR = CurrentUser(
    userId="seed-admin", roles=("admin",), departments=("procurement",)
)
_AGENT_CODE = "DELIVERY_TRACKING_AGENT"


def _legacyInstructions(agentCode: str) -> str:
    """该 agent 上一版种子的完整指令（旧公共块 + 同一段职责文案）。"""
    return _legacyInstructionsFor(agentCode)


async def _seedProfile(dbSession: AsyncSession, instructions: str) -> AgentOrchestrationProfile:
    """造一个已存在的 profile（不启用：启用要单独一步，与本用例无关）。

    先跑 `seedAgents` 建全部采购场景 Agent + 工具元数据（profile 种子要求
    每个场景 Agent 都存在，缺一个就抛错）。
    """
    from scripts.seed_agents import seedAgents

    await seedAgents(dbSession)
    await dbSession.commit()

    agent = (
        await dbSession.execute(
            select(AgentDefinition).where(AgentDefinition.agent_code == _AGENT_CODE)
        )
    ).scalar_one()
    profile = AgentOrchestrationProfile(
        agent_id=agent.id,
        instructions=instructions,
        enabled=False,
        default_max_iterations=12,
        default_max_tool_calls=18,
        default_max_cost_usd=5,
        default_timeout_seconds=900,
        max_observation_chars=50000,
        revision=1,
    )
    dbSession.add(profile)
    await dbSession.flush()
    await dbSession.commit()
    return profile


@pytest.mark.asyncio
async def test_legacy_instructions_helper_matches_seed_shape() -> None:
    """推导出的旧版指令 = 旧公共块 + 当前职责段（职责段两版一致）。"""
    current = PROFILE_SEEDS[_AGENT_CODE][0]
    legacy = _legacyInstructions(_AGENT_CODE)

    assert legacy.startswith(_LEGACY_READONLY_BASE)
    assert current.startswith(_READONLY_BASE)
    # 职责段（公共块之后的部分）两版逐字相同
    assert legacy[len(_LEGACY_READONLY_BASE) :] == current[len(_READONLY_BASE) :]


@pytest.mark.asyncio
async def test_sync_refreshes_legacy_seed_instructions(dbSession: AsyncSession) -> None:
    """存量指令 = 上一版种子 ⇒ 公共块升级成新版（改种子落下去）。"""
    from scripts.seed_procurement_agent_profiles import seedProcurementAgentProfiles

    profile = await _seedProfile(dbSession, _legacyInstructions(_AGENT_CODE))

    await seedProcurementAgentProfiles(dbSession)
    await dbSession.commit()

    await dbSession.refresh(profile)
    assert profile.instructions == PROFILE_SEEDS[_AGENT_CODE][0]
    assert "内部术语" in profile.instructions


@pytest.mark.asyncio
async def test_sync_preserves_older_duty_text(dbSession: AsyncSession) -> None:
    """**真实库的主要情形**：职责段是更早版本的种子 ⇒ 只换公共块，职责段一字不动。

    2026-11-26 实测：真实库 7 个采购 Agent 里 6 个的职责段比当前种子旧
    （后续批次改过职责文案）⇒ 整段比对永远不相等 ⇒ 修复静默落不下去。
    改为前缀替换后这些 Agent 也能升级。
    """
    from scripts.seed_procurement_agent_profiles import seedProcurementAgentProfiles

    staleDuty = "\n职责：到货跟踪（旧版职责文案，历史遗留，应当原样保留）。\n"
    profile = await _seedProfile(dbSession, _LEGACY_READONLY_BASE + staleDuty)

    await seedProcurementAgentProfiles(dbSession)
    await dbSession.commit()

    await dbSession.refresh(profile)
    # 公共块换成新版（带禁令）……
    assert profile.instructions.startswith(_READONLY_BASE)
    assert "内部术语" in profile.instructions
    # ……职责段（旧版文案）原样保留
    assert profile.instructions.endswith(staleDuty)
    assert profile.instructions == _READONLY_BASE + staleDuty


@pytest.mark.asyncio
async def test_sync_leaves_already_current_instructions(dbSession: AsyncSession) -> None:
    """存量指令 = 当前版 ⇒ 不动（幂等）。"""
    from scripts.seed_procurement_agent_profiles import seedProcurementAgentProfiles

    profile = await _seedProfile(dbSession, PROFILE_SEEDS[_AGENT_CODE][0])

    await seedProcurementAgentProfiles(dbSession)
    await dbSession.commit()

    await dbSession.refresh(profile)
    assert profile.instructions == PROFILE_SEEDS[_AGENT_CODE][0]


@pytest.mark.asyncio
async def test_sync_leaves_admin_edited_instructions(dbSession: AsyncSession) -> None:
    """管理员改过的指令（既不等于旧种子也不等于新版）⇒ 一律不动。"""
    from scripts.seed_procurement_agent_profiles import seedProcurementAgentProfiles

    custom = "你是采购域的分析助手。这是管理员定制过的指令，请原样保留。"
    profile = await _seedProfile(dbSession, custom)

    await seedProcurementAgentProfiles(dbSession)
    await dbSession.commit()

    await dbSession.refresh(profile)
    assert profile.instructions == custom
