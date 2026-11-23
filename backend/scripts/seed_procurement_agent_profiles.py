"""I3-a 采购场景 Agent 的编排配置（profile）种子脚本（幂等）。

背景：M3-3 注册了 3 个采购场景 Agent（`PROCUREMENT_*`），但**编排 profile**
是配置面数据，从来没种过 ⇒ `POST /agent-runs` 对它们一律 409
（「无启用的编排配置」），采购 Copilot 入口自然也就跑不起来。

本脚本为每个场景 Agent 建 profile（指令模板 + 工具组 + 预算上限），随后
**显式 toggle 启用**（profile 创建恒为 disabled 是既有设计，启用必须单独一步）。

幂等口径：
- profile 已存在 ⇒ 不覆盖（配置可能已被管理员改过，脚本不抢写），只打印状态；
- 缺 profile ⇒ 建 + 启用。

用法：
    cd backend && python -m scripts.seed_procurement_agent_profiles
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.agent_orchestration_models import AgentOrchestrationProfile  # noqa: E402
from app.domain.agent_orchestration_schemas import (  # noqa: E402
    AgentOrchestrationProfileCreate,
    AgentOrchestrationProfileUpdate,
    AgentOrchestrationToggle,
)
from app.domain.models import AgentDefinition  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.agent_orchestration_config_service import (  # noqa: E402
    AgentOrchestrationConfigService,
)

#: 种子 actor（与 seed_agents 同源口径：admin + 采购部门）。
_SEED_ACTOR = CurrentUser(
    userId="seed-admin", roles=("admin",), departments=("procurement",)
)

#: 公共约束（三条都写进指令：只读、必须引用证据、缺数要说不缺编）。
_READONLY_BASE = (
    "你是采购域的只读分析助手。铁律：\n"
    "1. 只能通过已绑定的工具取数，不得编造数据、不得猜测不存在的字段或表；\n"
    "2. 每条结论都要带上来源（工具名 + 命中的编码/单据号/文档片段）；\n"
    "3. 取不到数据时明确说「当前数据不足以回答」并指出缺哪一项，不要用常识补齐；\n"
    "4. 绝不执行任何写操作（询价、下单、审批、付款都不在你的能力范围内）。\n"
)

#: agent_code → (指令模板, 绑定工具, 迭代上限, 工具调用上限)
PROFILE_SEEDS: dict[str, tuple[str, list[str], int, int]] = {
    "PROCUREMENT_MATERIAL_SEARCH_AGENT": (
        _READONLY_BASE
        + "\n职责：物料检索。根据用户描述找到对应物料（编码/名称/规格），"
        "并回答该物料由哪些供应商可供（如工具支持）。回答先给命中实体，再给依据。",
        ["material_search", "policy_compliance_check"],
        10,
        15,
    ),
    "PROCUREMENT_SUPPLIER_SEARCH_AGENT": (
        _READONLY_BASE
        + "\n职责：供应商检索与 360 视图。按名称/编码定位供应商，"
        "汇总其基础信息、供货关系与风险信号；多候选时列出候选并让用户确认。"
        "\n定位顺序：用户只给**品类/物料描述**（如「外购标准件」）时，先调 "
        "material_search 把品类解析成具体物料编码，再用供应商检索工具的 "
        "materialKey 反查可供供应商；只有物料也解析不出来时才反问用户。",
        ["material_search", "supplier_search", "supplier_360"],
        10,
        15,
    ),
    "PROCUREMENT_QUOTATION_ANALYST": (
        _READONLY_BASE
        + "\n职责：报价比较。先按物料与年度取报价（quotation_query），再用"
        " quotation_compare 产出比价矩阵；回答里必须写明口径（CNY / 未税 / 同年度）"
        " 与偏差标记的含义，异常低价要提示人工复核，不替业务下结论。",
        ["quotation_query", "quotation_compare"],
        12,
        18,
    ),
    "PROCUREMENT_SOURCING": (
        _READONLY_BASE
        + "\n职责：采购寻源。用户给物料或需求时，先定位物料 → 找可供供应商 →"
        " 逐个补充 360 视图/风险/历史采购，最后给出**候选清单 + 每家的依据与风险提示**；"
        "\n候选为空或数据缺失时，明确说明缺哪一环（物料没有供应商关系 / 主数据缺失），"
        "不要用常识补候选。"
        + "\n待到货分析：用户问「待到货/逾期未到/欠交」时，调 pending_delivery_query"
          "（时间窗/供应商/逾期天数从问题里取，缺省近 12 个月），按供应商汇总后给催货建议；"
          "答案里引用订单号与物料编码。",
        [
            "material_search",
            "supplier_search",
            "supplier_360",
            "supplier_risk",
            "graph_traverse",
            "purchase_history_query",
            "policy_compliance_check",
            "pending_delivery_query",
        ],
        15,
        20,
    ),
    "PROCUREMENT_CONTRACT_SEARCH_AGENT": (
        _READONLY_BASE
        + "\n职责：合同条款检索。围绕合同条款、付款/交付约定、违约责任回答，"
        "每条结论必须引用命中的合同文档片段与生效期；没有命中就说没有，不要套用常识。",
        ["contract_document_search", "policy_compliance_check"],
        10,
        15,
    ),
    "PROCUREMENT_PURCHASE_HISTORY_AGENT": (
        _READONLY_BASE
        + "\n职责：采购历史。按供应商/物料查询历史采购与报价记录，"
        "回答时给出统计口径（时间窗、条数）与具体单据号，便于复核。",
        ["purchase_history_query", "quotation_query"],
        15,
        20,
    ),
}


async def _getProfile(
    session: AsyncSession, agentId: int
) -> AgentOrchestrationProfile | None:
    stmt = select(AgentOrchestrationProfile).where(
        AgentOrchestrationProfile.agent_id == agentId
    )
    return (await session.execute(stmt)).scalars().first()


async def _getAgent(session: AsyncSession, agentCode: str) -> AgentDefinition | None:
    stmt = select(AgentDefinition).where(AgentDefinition.agent_code == agentCode)
    return (await session.execute(stmt)).scalars().first()


async def _reconcileToolBindings(
    session: AsyncSession,
    service: AgentOrchestrationConfigService,
    agentCode: str,
    existing: AgentOrchestrationProfile,
    requiredTools: list[str],
) -> None:
    """把 `requiredTools` 中缺失的工具补进已有 profile（幂等，不动其他字段）。"""
    profile = await service.get(session, agentCode)
    current = {binding.tool_name for binding in profile.bindings}
    missing = [name for name in requiredTools if name not in current]
    if not missing:
        return
    merged = sorted(current | set(requiredTools))
    await service.update(
        session,
        agentCode,
        AgentOrchestrationProfileUpdate(
            instructions=profile.instructions,
            tool_names=merged,
            default_max_iterations=profile.default_max_iterations,
            default_max_tool_calls=profile.default_max_tool_calls,
            default_max_cost_usd=profile.default_max_cost_usd,
            default_timeout_seconds=profile.default_timeout_seconds,
            max_observation_chars=profile.max_observation_chars,
            revision=profile.revision,
        ),
        _SEED_ACTOR,
    )
    print(f"[seed_procurement_agent_profiles] {agentCode}: 补齐工具 {missing}")


async def seedProcurementAgentProfiles(session: AsyncSession) -> dict[str, str]:
    """为采购场景 Agent 建 profile 并启用；返回 {agent_code: 结果}。

    结果取值：`created+enabled` / `already-enabled` / `already-disabled`（不抢改）。
    缺 Agent 的条目抛错——静默跳过会让「Copilot 起不来」再次以 409 的面目出现。
    """
    service = AgentOrchestrationConfigService()
    outcome: dict[str, str] = {}
    for agentCode, (instructions, toolNames, maxIterations, maxToolCalls) in (
        PROFILE_SEEDS.items()
    ):
        agent = await _getAgent(session, agentCode)
        if agent is None:
            raise RuntimeError(
                f"场景 Agent {agentCode} 不存在；先跑 python -m scripts.seed_agents"
            )
        existing = await _getProfile(session, agent.id)
        if existing is not None:
            outcome[agentCode] = (
                "already-enabled" if existing.enabled else "already-disabled"
            )
            # 幂等补齐：把本清单里**新增**的工具补进已有 profile
            # （不覆盖管理员改过的其他字段；缺工具不补 ⇒ 新工具永远绑不上）
            await _reconcileToolBindings(
                session, service, agentCode, existing, toolNames
            )
            continue
        created = await service.create(
            session,
            AgentOrchestrationProfileCreate(
                agent_code=agentCode,
                instructions=instructions,
                tool_names=toolNames,
                default_max_iterations=maxIterations,
                default_max_tool_calls=maxToolCalls,
            ),
            _SEED_ACTOR,
        )
        await service.toggle(
            session,
            agentCode,
            AgentOrchestrationToggle(enabled=True, revision=created.revision),
            _SEED_ACTOR,
        )
        outcome[agentCode] = "created+enabled"
    return outcome


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        outcome = await seedProcurementAgentProfiles(session)
    for agentCode, result in outcome.items():
        print(f"[seed_procurement_agent_profiles] {agentCode}: {result}")


if __name__ == "__main__":
    asyncio.run(main())
