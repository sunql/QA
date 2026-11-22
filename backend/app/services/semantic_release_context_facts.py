"""发布校验的**活库事实**加载器（G2：支撑设计稿 §5.5 的 6 个校验码）。

为什么单独一个文件
------------------
「数据源还启用吗」「语义关系的端点类还在吗」「Agent 的域/层/工具还一致吗」
**只能实时查库** ⇒ 不属于纯函数层（`semantic_release_design_checks` 刻意保持无 IO），
但又是发布校验的必需输入。与 `procurement_binding_candidates` 同型：夹在
「校验项纯逻辑」与「编译纯函数」之间都会破坏单一职责 ⇒ 独立成模块。

口径
----
- **`None` = 没查过**：调用方不调用本模块时，校验项**不出行**（绝不写「全部正常」）；
- 只报**当前生效**的配置：`is_active` / `enabled` / `valid_to IS NULL` 之外的行属历史，
  不是待办债；
- 每个加载器返回**已排序、有上限**的违规描述串（detail 要可读，且不能让
  一次发布把响应体撑爆）。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.enums import AgentStatus, ClassRelationType
from app.domain.models import (
    AgentDefinition,
    AgentToolConfig,
    DataDomain,
    DataLayerRule,
    DataSource,
    OntologyClass,
    OntologyRelation,
)

#: 一次校验最多列多少条活库违规（detail 展示上限远小于此；这里防内存/响应体失控）。
MAX_CONTEXT_FACTS = 200

#: 作用域过滤时一次最多带多少个类名进 `IN`（与候选统计同口径）。
_MAX_SCOPE_CLASS_NAMES = 5000

#: 语义关系类型白名单（与 `neo4j_client.CLASS_RELATION_TYPES` 同源：枚举是 SSOT）。
_ALLOWED_RELATION_TYPES = frozenset(item.value for item in ClassRelationType)


async def loadActiveDatasourceNames(session: AsyncSession) -> frozenset[str]:
    """启用中的数据源**名字**集合（`MET-002` 的「名字还有效吗」判据）。"""
    statement = select(DataSource.name).where(DataSource.is_active.is_(True))
    names = (await session.execute(statement)).scalars().all()
    return frozenset(str(name) for name in names if name)


async def loadRelationViolations(
    session: AsyncSession, *, scopeClassNames: Sequence[str] | None = None
) -> tuple[str, ...]:
    """`ontology_relation` 的违规条目：类型不在白名单 / 端点类是墓碑。

    `scopeClassNames`：按发布快照里的类收窄（任一端命中即算），`None` = 全库口径。
    收窄后报告与快照同一宇宙（与候选统计同口径）。
    """
    source = aliased(OntologyClass)
    target = aliased(OntologyClass)
    statement = (
        select(
            OntologyRelation.relation_type,
            source.class_name,
            source.valid_to,
            target.class_name,
            target.valid_to,
        )
        .select_from(OntologyRelation)
        .join(source, source.id == OntologyRelation.source_class_id)
        .join(target, target.id == OntologyRelation.target_class_id)
    )
    scope = _scopeNames(scopeClassNames)
    if scope is not None:
        statement = statement.where(
            source.class_name.in_(scope) | target.class_name.in_(scope)
        )
    issues: list[str] = []
    for relationType, sourceName, sourceValidTo, targetName, targetValidTo in (
        await session.execute(statement)
    ).all():
        label = f"{sourceName}-[{relationType}]->{targetName}"
        if relationType not in _ALLOWED_RELATION_TYPES:
            issues.append(f"语义关系 {label} 的类型不在代码白名单")
        elif sourceValidTo is not None or targetValidTo is not None:
            issues.append(f"语义关系 {label} 的端点类已失效（墓碑）")
        if len(issues) >= MAX_CONTEXT_FACTS:
            break
    return tuple(sorted(issues))


async def loadAgentViolations(session: AsyncSession) -> tuple[str, ...]:
    """启用中 Agent 的域/层/工具一致性违规（设计稿 §5.5 `AGT-001`）。

    判据（只对 `status=active` 的 Agent —— 草稿/停用的本来就不可运行）：
    1. `data_domains` 的每一项都在**启用中**的 `data_domain` 词表内；
    2. `data_layers` 的每一项都在**启用中**的 `data_layer_rule` 层词表内；
    3. `tool_name` 非空且指向**已注册且启用**的 `agent_tool_config`。

    ⚠️ 未做「ACL 对齐」判据：`agent_access_policy` 与工具 `data_object` 的对齐
    需要在写入口收口时定义口径（当前设计稿只有一句「ACL 对齐」，没有可判定规则）；
    这里不发明规则，留作后续契约。
    """
    domains = set(
        (
            await session.execute(
                select(DataDomain.code).where(DataDomain.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    layers = set(
        (
            await session.execute(
                select(DataLayerRule.layer)
                .where(DataLayerRule.enabled.is_(True))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    tools = {
        str(name): bool(enabled)
        for name, enabled in (
            await session.execute(
                select(AgentToolConfig.name, AgentToolConfig.enabled)
            )
        ).all()
    }
    agents = (
        (
            await session.execute(
                select(AgentDefinition).where(
                    AgentDefinition.status == AgentStatus.ACTIVE.value
                )
            )
        )
        .scalars()
        .all()
    )
    issues: list[str] = []
    for agent in agents:
        issues.extend(_agentIssues(agent, domains, layers, tools))
        if len(issues) >= MAX_CONTEXT_FACTS:
            break
    return tuple(sorted(issues[:MAX_CONTEXT_FACTS]))


def _agentIssues(
    agent: AgentDefinition,
    domains: set[str],
    layers: set[str],
    tools: dict[str, bool],
) -> list[str]:
    code = agent.agent_code
    issues = [
        f"Agent {code} 的数据域 {name} 不在启用词表内"
        for name in _asList(agent.data_domains)
        if name not in domains
    ]
    issues.extend(
        f"Agent {code} 的数据层 {name} 不在启用词表内"
        for name in _asList(agent.data_layers)
        if name not in layers
    )
    issues.extend(_toolIssues(code, agent.tool_name, tools))
    return issues


def _toolIssues(code: str, toolName: str | None, tools: dict[str, bool]) -> list[str]:
    if not toolName:
        return [f"Agent {code} 未绑定工具（不可运行）"]
    if toolName not in tools:
        return [f"Agent {code} 绑定的工具 {toolName} 未注册"]
    if not tools[toolName]:
        return [f"Agent {code} 绑定的工具 {toolName} 已停用"]
    return []


def _asList(value: object) -> list[str]:
    """JSONB 数组 ⇒ 字符串列表（脏数据不炸：非列表当空，非串元素跳过）。"""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _scopeNames(scopeClassNames: Sequence[str] | None) -> tuple[str, ...] | None:
    """把作用域类名归一成 `IN` 列表（`None` = 不收窄）。"""
    if scopeClassNames is None:
        return None
    names = sorted({str(name) for name in scopeClassNames if str(name)})
    return tuple(names[:_MAX_SCOPE_CLASS_NAMES])
