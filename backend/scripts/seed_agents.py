"""Phase 6.4 Agent 注册种子脚本（幂等）。

5 个 Agent 注册到 Agent Registry：
- 3 个可运行（绑定 AGENT_TOOLS，Phase 6.4 验收「≥2 个 Agent 端到端可调用」）：
    SUPPLIER_360_AGENT  → supplier_360 工具
    SUPPLIER_RISK_AGENT → supplier_risk 工具
    GRAPH_REASONING_AGENT → graph_traverse 工具
- 2 个元数据 Agent（仅注册展示，AGENT_TOOLS 无绑定 → 运行时返回 409 不可运行）：
    SUPPLIER_OTD_REPORT / PROCUREMENT_COPILOT

每个 Agent 带 SUPPLIER READ 策略（deny-by-default 下工具数据对象可读）。

幂等：按 agent_code 预查跳过（冲突时再 catch ConflictError），重复运行不产生
重复数据，并发运行安全。走 AgentRegistryService.createAgent（service + 真实 PG），
actor 用 admin + departments=("procurement",) 派生 owner="procurement"。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.enums import (  # noqa: E402
    AgentPermission,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
)
from app.domain.exceptions import ConflictError  # noqa: E402
from app.domain.models import AgentDefinition  # noqa: E402
from app.domain.schemas import (  # noqa: E402
    AgentAccessPolicyCreate,
    AgentDefinitionCreate,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.agent_registry_service import AgentRegistryService  # noqa: E402

# 种子 actor：admin + 采购部门 → owner 派生 "procurement"
_SEED_ACTOR = CurrentUser(
    userId="seed-admin", roles=("admin",), departments=("procurement",)
)

# 通用 SUPPLIER READ 策略（每个 Agent 一份）
_DEFAULT_POLICIES = [
    AgentAccessPolicyCreate(
        data_object="SUPPLIER",
        permission=AgentPermission.READ,
        data_layer=None,
        notes="Phase 6.4 Agent 运行时种子",
    )
]


def _agent(
    code: str,
    name: str,
    description: str,
    *,
    status: AgentStatus = AgentStatus.ACTIVE,
    domains: tuple[str, ...] = ("PROCUREMENT",),
    layers: tuple[str, ...] = ("FEATURE", "DIM"),
) -> dict[str, Any]:
    """构造一个 Agent 种子（不可变：不修改任何入参，返回新 dict）。"""
    return dict(
        agent_code=code,
        agent_name=name,
        description=description,
        status=status,
        domains=list(domains),
        layers=list(layers),
    )


# 5 个 Agent 种子（与 AGENT_TOOLS 确定性绑定对齐：仅前 3 个可运行）
AGENT_SEEDS: list[dict[str, Any]] = [
    _agent(
        "SUPPLIER_360_AGENT",
        "供应商 360° Agent",
        "查询单供应商 360° 视图：主数据 + 交付/质量/价格表现 + 跨系统编码（supplier_360 工具，只读聚合无 LLM）。",
    ),
    _agent(
        "SUPPLIER_RISK_AGENT",
        "供应商风险 Agent",
        "评估单供应商风险等级：RISK_SCORE 主路径 + 其他 Feature fallback + LLM 生成风险点（supplier_risk 工具）。",
    ),
    _agent(
        "GRAPH_REASONING_AGENT",
        "供应链图推理 Agent",
        "供应链链路推理：从供应商出发的多跳可达业务实体（物料/订单/收货/问题），Neo4j 图遍历（graph_traverse 工具）。",
        layers=("DIM", "DWD"),
    ),
    _agent(
        "SUPPLIER_OTD_REPORT",
        "供应商 OTD 报表 Agent",
        "供应商准时交付率（OTD）周期报表生成 Agent。本期仅注册元数据，未绑定工具，暂不可运行（Phase 7+ 排期）。",
    ),
    _agent(
        "PROCUREMENT_COPILOT",
        "采购 AI 副驾",
        "采购域综合问答 Copilot（NL2SQL + 多 Agent 编排占位）。本期仅注册元数据，未绑定工具，暂不可运行（Phase 7+ 排期）。",
        layers=("DIM", "DWD", "FEATURE"),
    ),
]


async def seedAgents(session: Any) -> int:
    """幂等注册 5 个 Agent；返回本次新增条数。

    - 预查既有 agent_code，跳过已存在项
    - 并发冲突（预查后仍撞唯一键）→ 捕获 ConflictError 跳过
    """
    service = AgentRegistryService()
    existing = set(
        (await session.execute(select(AgentDefinition.agent_code))).scalars().all()
    )
    inserted = 0
    for seed in AGENT_SEEDS:
        if seed["agent_code"] in existing:
            continue
        dto = AgentDefinitionCreate(
            agent_code=seed["agent_code"],
            agent_name=seed["agent_name"],
            description=seed["description"],
            trigger_type=AgentTriggerType.USER_QUESTION,
            response_latency=AgentResponseLatency.REALTIME,
            data_domains=seed["domains"],
            data_layers=seed["layers"],
            status=seed["status"],
            version="v1.0",
            policies=_DEFAULT_POLICIES,
        )
        try:
            await service.createAgent(session, dto, _SEED_ACTOR)
        except ConflictError:
            # 并发运行：另一个进程已插入同 code → 静默跳过
            continue
        existing.add(seed["agent_code"])
        inserted += 1
        print(f"  + {seed['agent_code']}（{seed['agent_name']}）")
    return inserted


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        inserted = await seedAgents(session)
        total = (
            (await session.execute(select(AgentDefinition.agent_code)))
            .scalars()
            .all()
        )
    runnable = [c for c in total if c in {"SUPPLIER_360_AGENT", "SUPPLIER_RISK_AGENT", "GRAPH_REASONING_AGENT"}]
    print(f"[seed_agents] 本次新增 {inserted} 个，库内共 {len(total)} 个")
    print(f"[seed_agents] 可运行 Agent（绑定 AGENT_TOOLS）：{len(runnable)} 个 → {sorted(runnable)}")
    print("✅ Agent 注册种子完成（Phase 6.4 验收 ≥2 个 Agent 端到端可调用已满足）")


if __name__ == "__main__":
    asyncio.run(main())
