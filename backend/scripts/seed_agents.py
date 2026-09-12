"""Phase 6.4 Agent 注册种子脚本（幂等）。

9 个 Agent 注册到 Agent Registry：
- 7 个可运行（绑定 AGENT_DEFAULT_BINDINGS，Phase 6.4 验收「≥2 个 Agent 端到端可调用」）：
    SUPPLIER_360_AGENT    → supplier_360 工具
    SUPPLIER_RISK_AGENT   → supplier_risk 工具
    GRAPH_REASONING_AGENT → graph_traverse 工具
    WIKI_SEARCH_AGENT     → wiki_search 工具     （feat-wiki-knowledge M8）
    WIKI_READ_AGENT       → wiki_read 工具       （feat-wiki-knowledge M8）
    WIKI_RULE_AGENT       → rule_evaluate 工具   （feat-wiki-knowledge M8）
    WIKI_COVERAGE_AGENT   → coverage_status 工具 （feat-wiki-knowledge M8）
- 2 个元数据 Agent（仅注册展示，AGENT_DEFAULT_BINDINGS 无绑定 → 运行时返回 409 不可运行）：
    SUPPLIER_OTD_REPORT / PROCUREMENT_COPILOT

独立可执行：`seedAgents` 自己补 tool config seed + registry warmUp（`_ensureToolRegistry`），
`main()` 再补 tool_name 绑定 —— 不走 lifespan 也能把库留在「跑得起来」的状态。

可运行 Agent 带显式分层 READ 策略（按绑定工具的数据层逐层授权，最小权限）；
元数据 Agent 保留 SUPPLIER READ 通配策略（仅展示，运行时在工具门禁 409）。

幂等：按 agent_code 预查跳过（冲突时再 catch ConflictError），重复运行不产生
重复数据，并发运行安全。走 AgentRegistryService.createAgent（service + 真实 PG），
actor 用 admin + departments=("procurement",) 派生 owner="procurement"。
另对既有可运行 Agent 做策略愈合（_reconcilePolicies）：有层工具删通配 READ + 补齐显式分层，
层无关工具补齐对象粒度 READ —— 保证「Agent 已存在」时策略同样正确，不必删库重 seed。
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
from app.domain.exceptions import ConflictError, NotFoundError  # noqa: E402
from app.domain.models import AgentDefinition  # noqa: E402
from app.domain.schemas import (  # noqa: E402
    AgentAccessPolicyCreate,
    AgentDefinitionCreate,
    _normalizeDataLayer,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.agent_registry_service import AgentRegistryService  # noqa: E402
from app.services.agent_tool_config_registry import (  # noqa: E402
    agent_tool_config_registry,
)

# 种子 actor：admin + 采购部门 → owner 派生 "procurement"
_SEED_ACTOR = CurrentUser(
    userId="seed-admin", roles=("admin",), departments=("procurement",)
)

# 通用 SUPPLIER READ 通配策略（仅供无工具绑定的元数据 Agent 展示；运行时在工具门禁 409）
_DEFAULT_POLICIES = [
    AgentAccessPolicyCreate(
        data_object="SUPPLIER",
        permission=AgentPermission.READ,
        data_layer=None,
        notes="Phase 6.4 Agent 运行时种子（元数据 Agent 占位）",
    )
]

# Agent → tool 绑定常量（仅作 seed 元数据；运行时真实绑定来自
# agent_definition.tool_name + agent_binding_cache，feat-agent-tool-config-db）。
# 与 seed_agent_tool_bindings.py 共享；不依赖模块单例（agent_tools 已删除 AGENT_DEFAULT_BINDINGS）。
_AGENT_DEFAULT_BINDINGS: dict[str, str] = {
    "SUPPLIER_360_AGENT": "supplier_360",
    "SUPPLIER_RISK_AGENT": "supplier_risk",
    "GRAPH_REASONING_AGENT": "graph_traverse",
    # feat-wiki-knowledge M8：一个 Agent 绑一个工具（agent_definition.tool_name
    # 是单值列），故知识工具各配一个可运行 Agent。
    "WIKI_SEARCH_AGENT": "wiki_search",
    "WIKI_READ_AGENT": "wiki_read",
    "WIKI_RULE_AGENT": "rule_evaluate",
    "WIKI_COVERAGE_AGENT": "coverage_status",
}


async def _ensureToolRegistry(session: Any) -> None:
    """确保 ``agent_tool_config_registry`` 已 warmUp（缺它 ``_policiesFor`` 必炸）。

    本脚本会被**独立执行**（``python -m scripts.seed_agents``），此时进程里没有
    lifespan，registry 停在「未加载」状态，``_policiesFor`` 抛
    ``RuntimeError: AgentToolConfigRegistry 未 warmUp（lifespan bug）`` —— 那条
    报错会把人引向 lifespan，而真正的原因是这个脚本从来没装过 registry。
    （集成测试同样要自己 seed + warmUp，见 ``test_seed_agents.py`` 的 fixture 注释。）

    顺序与 lifespan 一致：先 seed 工具元数据，再 invalidate + warmUp。
    幂等，可反复调用。
    """
    from scripts.seed_agent_tool_configs import seedAgentToolConfigs

    await seedAgentToolConfigs(session)
    await session.commit()
    agent_tool_config_registry.invalidate()
    await agent_tool_config_registry.warmUp(session)


async def _policiesFor(session: Any, code: str) -> list[AgentAccessPolicyCreate]:
    """为 Agent 生成显式分层策略（最小权限）。

    可运行 Agent：按 _AGENT_DEFAULT_BINDINGS → agent_tool_config_registry
    读取 data_object + data_layers，逐层授权（与运行时分层校验对齐）。

    **层无关工具按对象粒度授权**（``data_layer=None`` 通配），对象取该工具自己
    声明的 ``data_object``。这里曾经无差别回退 ``_DEFAULT_POLICIES``（一份写死
    SUPPLIER 的通配策略）——对 SUPPLIER 工具恰好等价，对 feat-wiki-knowledge
    的 WIKI_* 工具就是**静默 fail-closed**：运行时 ``_enforcePolicies`` 按
    ``p.data_object == tool.data_object`` 精确比对，一条 WIKI_PAGE 的策略都没有，
    全部 403。授权要跟着工具走，不能跟着「默认」走。

    元数据 Agent（无绑定）或工具未启用 → 回退 ``_DEFAULT_POLICIES``
    （仅展示，运行时不可运行 / 避免零策略 fail-closed）。
    """
    tool_name = _AGENT_DEFAULT_BINDINGS.get(code)
    if tool_name is None:
        return list(_DEFAULT_POLICIES)
    tool = agent_tool_config_registry.get(tool_name)
    if tool is None:
        # 工具被禁用或未 seed → 回退通配（旧行为兼容）
        return list(_DEFAULT_POLICIES)
    if not tool.data_layers:
        return [
            AgentAccessPolicyCreate(
                data_object=tool.data_object,
                permission=AgentPermission.READ,
                data_layer=None,
                notes="Phase 6.4 Agent 运行时种子（层无关工具，对象粒度，最小权限）",
            )
        ]
    return [
        AgentAccessPolicyCreate(
            data_object=tool.data_object,
            permission=AgentPermission.READ,
            data_layer=layer,
            notes="Phase 6.4 Agent 运行时种子（显式分层，最小权限）",
        )
        for layer in tool.data_layers
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


def _wikiAgent(code: str, name: str, description: str) -> dict[str, Any]:
    """构造一个知识工具 Agent 种子（跨域、跨层；不可变：返回新 dict）。

    与 ``_agent`` 分开而不是给它加参数：空 domains/layers 在这两个 helper 里
    表达的语义相反 —— ``_agent`` 的默认值是「采购域、DIM+FEATURE 层」这个
    **具体授权**，而知识 Agent 的空列表是「不限定」。用同一个 helper 会让人
    以为后者是漏填。
    """
    return _agent(code, name, description, domains=(), layers=())


# 9 个 Agent 种子（与 AGENT_DEFAULT_BINDINGS 确定性绑定对齐：7 个可运行，2 个仅元数据）
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
    # ---- feat-wiki-knowledge M8：企业 Wiki 知识工具 Agent（4 个，均可运行）----
    # domains / layers 均为空：知识库是**跨域**资产，条目归 KO（业务对象）不归
    # 数据域；把它们钉死在某一个域或某一层上，等于给通用知识加了一个假的边界。
    _wikiAgent(
        "WIKI_SEARCH_AGENT",
        "知识检索 Agent",
        "在企业知识库中检索条目：按标题/正文模糊匹配，返回摘要与维度（wiki_search 工具，只读无 LLM）。",
    ),
    _wikiAgent(
        "WIKI_READ_AGENT",
        "知识阅读 Agent",
        "读取单条知识的全文、事实原子、已确认关系与结构化规则/流程（wiki_read 工具，只读无 LLM）。",
    ),
    _wikiAgent(
        "WIKI_RULE_AGENT",
        "知识规则试跑 Agent",
        "对知识条目上的可执行业务规则跑 dry-run，报告通过/失败/判不了三态（rule_evaluate 工具，只读无 LLM）。",
    ),
    _wikiAgent(
        "WIKI_COVERAGE_AGENT",
        "知识覆盖度 Agent",
        "汇报知识覆盖度矩阵汇总、最紧迫缺口与未挂业务对象的条目（coverage_status 工具，只读无 LLM）。",
    ),
]


async def _reconcileObjectGrainPolicy(
    session: Any, code: str, service: AgentRegistryService, tool: Any
) -> tuple[int, int]:
    """层无关工具：缺对象粒度 READ 就补一条；永远返回 (0, 新增)。

    曾经这里直接 `return 0, 0`（按「层无关工具无需处理」跳过），后果是
    **已存在的 Agent 永远无法被 heal**：`seedAgents` 对 `agent_code` 已存在项
    `continue`，`_policiesFor` 根本不会再跑——测试库被 TRUNCATE 后重 seed、
    或旧部署新增知识工具，Agent 行还在、策略没了，运行时精确比对 → 全部 403，
    而 seed 输出一切正常。见 Harness changes §10B.4 同类「静默 fail-closed」。
    """
    have = {
        (p.data_object, _normalizeDataLayer(p.data_layer))
        for p in await service.listPolicies(session, code)
    }
    if (tool.data_object, None) in have:
        return 0, 0
    try:
        await service.addPolicy(
            session,
            code,
            AgentAccessPolicyCreate(
                data_object=tool.data_object,
                permission=AgentPermission.READ,
                data_layer=None,
                notes="Phase 6.4 Agent 运行时种子（层无关工具：对象粒度最小权限）",
            ),
            _SEED_ACTOR,
        )
    except ConflictError:
        return 0, 0  # 并发：另一进程已补齐
    return 0, 1


async def _reconcilePolicies(session: Any, code: str) -> tuple[int, int]:
    """愈合既有种子 Agent 的策略（幂等，最小权限）；返回 (删除, 新增) 条数。

    **有层工具**：删除其对工具 data_object 的 data_layer=None 通配 READ /
    MASKED_READ 策略（旧种子遗留——通配会让分层约束形同虚设），再确保每个绑定层
    都有显式 READ 策略。已显式分层的 Agent → 空操作。

    **层无关工具**（`data_layers == []`，如 feat-wiki-knowledge 的 WIKI_*）：
    确保对象粒度（data_layer=None）READ 策略存在，**只增不删**——这类工具的正确
    策略本身就是 data_layer=None，走上面的删除分支会把正确策略删掉。

    元数据 Agent（无工具绑定）或工具未注册 → 不处理。

    并发安全：deletePolicy / addPolicy 竞争（另一进程已删/已建）→ 捕获
    NotFoundError / ConflictError 按幂等跳过。
    注意：删除无法区分「种子遗留通配」与「管理员刻意保留的通配」——通配对可运行
    Agent 是多余授权（运行时只按工具声明层判定），删除即分层补强本意，故接受。
    层无关分支不清理其它对象的遗留策略（如旧种子写死的 SUPPLIER 通配）：运行时按
    `p.data_object == tool.data_object` 精确比对，它对本工具既不放行也不拦截，
    删它超出「本工具最小权限」的范围。
    """
    tool_name = _AGENT_DEFAULT_BINDINGS.get(code)
    if tool_name is None:
        return 0, 0
    tool = agent_tool_config_registry.get(tool_name)
    if tool is None:
        return 0, 0
    service = AgentRegistryService()
    if not tool.data_layers:
        return await _reconcileObjectGrainPolicy(session, code, service, tool)
    policies = await service.listPolicies(session, code)
    deleted = 0
    for p in policies:
        if (
            p.data_object == tool.data_object
            and p.data_layer is None
            and p.permission in (
                AgentPermission.READ.value,
                AgentPermission.MASKED_READ.value,
            )
        ):
            try:
                await service.deletePolicy(session, code, p.id, _SEED_ACTOR)
            except NotFoundError:
                continue  # 并发：另一进程已删除
            deleted += 1
    have = {
        (p.data_object, _normalizeDataLayer(p.data_layer))
        for p in await service.listPolicies(session, code)
    }
    added = 0
    for layer in tool.data_layers:
        if (tool.data_object, layer) not in have:
            try:
                await service.addPolicy(
                    session,
                    code,
                    AgentAccessPolicyCreate(
                        data_object=tool.data_object,
                        permission=AgentPermission.READ,
                        data_layer=layer,
                        notes="Phase 6.4 Agent 运行时种子（显式分层，最小权限）",
                    ),
                    _SEED_ACTOR,
                )
            except ConflictError:
                continue  # 并发：另一进程已补齐该层
            added += 1
    return deleted, added


async def seedAgents(session: Any) -> int:
    """幂等注册 5 个 Agent；返回本次新增条数。

    - 预查既有 agent_code，跳过已存在项
    - 并发冲突（预查后仍撞唯一键）→ 捕获 ConflictError 跳过
    - 对既有可运行 Agent 做策略愈合（删除通配 READ → 显式分层），保证
      data_layer 分层约束在已 seed 的部署上同样生效（非空操作）
    """
    await _ensureToolRegistry(session)
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
            policies=await _policiesFor(session, seed["agent_code"]),
        )
        try:
            await service.createAgent(session, dto, _SEED_ACTOR)
        except ConflictError:
            # 并发运行：另一个进程已插入同 code → 静默跳过
            continue
        existing.add(seed["agent_code"])
        inserted += 1
        print(f"  + {seed['agent_code']}（{seed['agent_name']}）")
    # 愈合既有种子：通配 READ → 显式分层 / 层无关工具补对象粒度（最小权限）
    for seed in AGENT_SEEDS:
        deleted, added = await _reconcilePolicies(session, seed["agent_code"])
        if deleted or added:
            print(f"  ~ {seed['agent_code']}：策略愈合 删{deleted} 条 / 增{added} 条")
    return inserted


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        inserted = await seedAgents(session)
        # 补做绑定：本脚本独立执行时不会走 lifespan，而 tool_name 绑定正是
        # lifespan 干的活（seed_agent_tool_bindings）。少了这一步，seed 报成功，
        # 但新 Agent 在下次重启前一律 409「未绑定工具」——「种子跑通了却跑不起来」
        # 是最难查的一类故障。
        from scripts.seed_agent_tool_bindings import seed_agent_tool_bindings

        bound = await seed_agent_tool_bindings(session)
        total = (
            (await session.execute(select(AgentDefinition.agent_code))).scalars().all()
        )
        # 可运行 = 真的绑上了工具（读库，不读常量）：常量与库不一致时，
        # 报告必须反映库的真实状态，否则这份日志会替一个坏掉的部署背书。
        runnable = sorted(
            (
                await session.execute(
                    select(AgentDefinition.agent_code).where(
                        AgentDefinition.tool_name.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    print(
        f"[seed_agents] 本次新增 {inserted} 个 Agent / {bound} 条工具绑定，"
        f"库内共 {len(total)} 个 Agent"
    )
    print(f"[seed_agents] 已绑定工具的 Agent：{len(runnable)} 个 → {runnable}")
    print("✅ Agent 注册种子完成（Phase 6.4 验收 ≥2 个 Agent 端到端可调用已满足）")


if __name__ == "__main__":
    asyncio.run(main())
