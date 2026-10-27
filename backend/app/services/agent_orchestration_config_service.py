"""Agent orchestration 配置服务（Task 3）。

职责：
- profile 的 create / get / list / update / toggle；
- 只读工具选项（``listToolOptions``）——仅暴露启用、schema 合法且命中
  ``READ_ONLY_HANDLER_REFS`` 白名单的 BUILTIN 工具；
- 写操作统一走 ``AclService.assertCanModify``（admin 或 owner 部门）；
- 乐观锁：update / toggle 携带 ``revision``，不匹配 → 409；
- 审计经 ``OutboxService`` 与业务写入同事务提交（before/after，不含密钥）。

设计约束：
- profile 创建恒为 ``enabled=False``（DTO 不允许声明 enabled），启用必须显式 toggle；
- 绑定工具在写入时逐项校验（存在 / enabled / BUILTIN / 白名单 / schema 合法）；
- 预算上限不复制到 DTO，统一对照 ``Settings`` 的系统天花板。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.agent_orchestration_models import (
    AgentOrchestrationBinding,
    AgentOrchestrationProfile,
)
from app.domain.agent_orchestration_schemas import (
    AgentOrchestrationBindingRead,
    AgentOrchestrationProfileCreate,
    AgentOrchestrationProfileRead,
    AgentOrchestrationProfileUpdate,
    AgentOrchestrationToggle,
)
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AgentDefinition, AgentToolConfig
from app.services.acl_service import ADMIN_ROLE, AclService
from app.services.agent_orchestration_validation import (
    BUILTIN_HANDLER_KIND,
    READ_ONLY_HANDLER_REFS,
    assertOrchestrationToolValid,
    assertWithinSystemCeilings,
)
from app.services.agent_registry_service import AgentRegistryService
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)

_AUDIT_EVENT_PREFIX = "agent_orchestration_profile"
_AUDIT_ENTITY_TYPE = "agent_orchestration_profile"

# profile DTO 字段 → 共享预算校验的规范字段名（错误信息仍显示 DTO 字段名）。
_PROFILE_LIMIT_LABELS = {
    "max_iterations": "default_max_iterations",
    "max_tool_calls": "default_max_tool_calls",
    "max_cost_usd": "default_max_cost_usd",
    "timeout_seconds": "default_timeout_seconds",
    "max_observation_chars": "max_observation_chars",
}


def _profileLimits(
    dto: AgentOrchestrationProfileCreate | AgentOrchestrationProfileUpdate,
) -> dict[str, float]:
    """profile 预算 → 共享预算校验的规范字段。"""
    return {
        "max_iterations": dto.default_max_iterations,
        "max_tool_calls": dto.default_max_tool_calls,
        "max_cost_usd": float(dto.default_max_cost_usd),
        "timeout_seconds": dto.default_timeout_seconds,
        "max_observation_chars": dto.max_observation_chars,
    }


def _profileToRead(
    profile: AgentOrchestrationProfile,
    agentCode: str,
    bindings: list[AgentOrchestrationBinding],
) -> AgentOrchestrationProfileRead:
    """ORM → 读模型；bindings 按 sort_order 稳定排序。"""
    ordered = sorted(bindings, key=lambda binding: (binding.sort_order, binding.tool_name))
    return AgentOrchestrationProfileRead(
        agent_code=agentCode,
        instructions=profile.instructions,
        enabled=profile.enabled,
        default_max_iterations=profile.default_max_iterations,
        default_max_tool_calls=profile.default_max_tool_calls,
        default_max_cost_usd=float(profile.default_max_cost_usd),
        default_timeout_seconds=profile.default_timeout_seconds,
        max_observation_chars=profile.max_observation_chars,
        revision=profile.revision,
        bindings=[
            AgentOrchestrationBindingRead(
                tool_name=binding.tool_name,
                enabled=binding.enabled,
                sort_order=binding.sort_order,
            )
            for binding in ordered
        ],
    )


def _profileToAuditDict(
    profile: AgentOrchestrationProfile,
    bindings: list[AgentOrchestrationBinding],
) -> dict:
    """审计 before/after 载荷（无密钥；只含配置面字段）。"""
    ordered = sorted(bindings, key=lambda binding: (binding.sort_order, binding.tool_name))
    return {
        "id": profile.id,
        "agentId": profile.agent_id,
        "instructions": profile.instructions,
        "enabled": profile.enabled,
        "defaultMaxIterations": profile.default_max_iterations,
        "defaultMaxToolCalls": profile.default_max_tool_calls,
        "defaultMaxCostUsd": float(profile.default_max_cost_usd),
        "defaultTimeoutSeconds": profile.default_timeout_seconds,
        "maxObservationChars": profile.max_observation_chars,
        "revision": profile.revision,
        "bindings": [
            {
                "toolName": binding.tool_name,
                "enabled": binding.enabled,
                "sortOrder": binding.sort_order,
            }
            for binding in ordered
        ],
    }


class AgentOrchestrationConfigService:
    """编排 profile 配置与只读工具选项。"""

    def __init__(
        self,
        *,
        agents: AgentRegistryService | None = None,
        acl: AclService | None = None,
        outbox: OutboxService | None = None,
    ) -> None:
        self._agents = agents or AgentRegistryService()
        self._acl = acl or AclService()
        self._outbox = outbox or OutboxService()

    # ------------------------------------------------------------------
    # 写：create / update / toggle
    # ------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        dto: AgentOrchestrationProfileCreate,
        actor: CurrentUser,
    ) -> AgentOrchestrationProfileRead:
        agent = await self._agents.getAgent(session, dto.agent_code)
        self._acl.assertCanModify(actor, agent.owner, "Agent", dto.agent_code)
        self._assertWithinSystemCeilings(dto)
        toolNames = _dedupeToolNames(dto.tool_names)
        tools = await self._loadBindableTools(session, toolNames)
        await self._assertNoProfile(session, agent.id, dto.agent_code)

        profile = AgentOrchestrationProfile(
            agent_id=agent.id,
            instructions=dto.instructions,
            enabled=False,
            default_max_iterations=dto.default_max_iterations,
            default_max_tool_calls=dto.default_max_tool_calls,
            default_max_cost_usd=Decimal(str(dto.default_max_cost_usd)),
            default_timeout_seconds=dto.default_timeout_seconds,
            max_observation_chars=dto.max_observation_chars,
            revision=1,
        )
        session.add(profile)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                f"Agent {dto.agent_code} 已存在编排配置"
            ) from exc

        bindings = _buildBindings(profile.id, tools)
        session.add_all(bindings)
        await session.flush()
        await self._enqueueAudit(
            session,
            profile,
            bindings,
            actor,
            eventAction="created",
            before=None,
        )
        await session.commit()
        return _profileToRead(profile, dto.agent_code, bindings)

    async def update(
        self,
        session: AsyncSession,
        agentCode: str,
        dto: AgentOrchestrationProfileUpdate,
        actor: CurrentUser,
    ) -> AgentOrchestrationProfileRead:
        agent = await self._agents.getAgent(session, agentCode)
        self._acl.assertCanModify(actor, agent.owner, "Agent", agentCode)
        profile = await self._getProfile(session, agent.id, agentCode)
        _assertRevision(profile.revision, dto.revision)
        self._assertWithinSystemCeilings(dto)
        toolNames = _dedupeToolNames(dto.tool_names)
        tools = await self._loadBindableTools(session, toolNames)

        oldBindings = await self._loadBindings(session, profile.id)
        before = _profileToAuditDict(profile, oldBindings)

        profile.instructions = dto.instructions
        profile.default_max_iterations = dto.default_max_iterations
        profile.default_max_tool_calls = dto.default_max_tool_calls
        profile.default_max_cost_usd = Decimal(str(dto.default_max_cost_usd))
        profile.default_timeout_seconds = dto.default_timeout_seconds
        profile.max_observation_chars = dto.max_observation_chars
        profile.revision += 1

        await session.execute(
            delete(AgentOrchestrationBinding).where(
                AgentOrchestrationBinding.profile_id == profile.id
            )
        )
        await session.flush()
        bindings = _buildBindings(profile.id, tools)
        session.add_all(bindings)
        await session.flush()

        await self._enqueueAudit(
            session,
            profile,
            bindings,
            actor,
            eventAction="updated",
            before=before,
        )
        await session.commit()
        return _profileToRead(profile, agentCode, bindings)

    async def toggle(
        self,
        session: AsyncSession,
        agentCode: str,
        dto: AgentOrchestrationToggle,
        actor: CurrentUser,
    ) -> AgentOrchestrationProfileRead:
        agent = await self._agents.getAgent(session, agentCode)
        self._acl.assertCanModify(actor, agent.owner, "Agent", agentCode)
        profile = await self._getProfile(session, agent.id, agentCode)
        _assertRevision(profile.revision, dto.revision)

        bindings = await self._loadBindings(session, profile.id)
        before = _profileToAuditDict(profile, bindings)

        profile.enabled = dto.enabled
        profile.revision += 1
        await session.flush()

        await self._enqueueAudit(
            session,
            profile,
            bindings,
            actor,
            eventAction="updated",
            before=before,
        )
        await session.commit()
        return _profileToRead(profile, agentCode, bindings)

    # ------------------------------------------------------------------
    # 读：get / list / tool options
    # ------------------------------------------------------------------

    async def get(
        self,
        session: AsyncSession,
        agentCode: str,
    ) -> AgentOrchestrationProfileRead:
        agent = await self._agents.getAgent(session, agentCode)
        profile = await self._getProfile(session, agent.id, agentCode)
        bindings = await self._loadBindings(session, profile.id)
        return _profileToRead(profile, agentCode, bindings)

    async def list(
        self,
        session: AsyncSession,
        actor: CurrentUser,
    ) -> list[AgentOrchestrationProfileRead]:
        """列出 actor 可见的 profile：admin 全部；否则 owner 部门匹配。"""
        stmt = select(
            AgentOrchestrationProfile, AgentDefinition.agent_code
        ).join(
            AgentDefinition,
            AgentOrchestrationProfile.agent_id == AgentDefinition.id,
        )
        if ADMIN_ROLE not in actor.roles:
            stmt = stmt.where(
                AgentDefinition.owner.in_(tuple(actor.departments or ()))
            )
        stmt = stmt.order_by(AgentDefinition.agent_code)
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []

        bindingsByProfile = await self._loadBindingsForProfiles(
            session, [profile.id for profile, _ in rows]
        )
        return [
            _profileToRead(profile, agentCode, bindingsByProfile.get(profile.id, []))
            for profile, agentCode in rows
        ]

    async def listToolOptions(self, session: AsyncSession) -> list[dict]:
        """启用 + BUILTIN + 只读白名单 + schema 合法的工具选项（按 name 排序）。

        非法 schema 行被静默省略（warning 只含工具名，不泄漏 schema 内容）。
        """
        rows = (
            (
                await session.execute(
                    select(AgentToolConfig)
                    .where(
                        AgentToolConfig.enabled.is_(True),
                        AgentToolConfig.handler_kind == BUILTIN_HANDLER_KIND,
                        AgentToolConfig.handler_ref.in_(
                            sorted(READ_ONLY_HANDLER_REFS)
                        ),
                    )
                    .order_by(AgentToolConfig.name)
                )
            )
            .scalars()
            .all()
        )

        options: list[dict] = []
        for row in rows:
            try:
                assertOrchestrationToolValid(row, row.name)
            except ValidationError:
                logger.warning(
                    "agent orchestration tool option omitted "
                    "(invalid input schema): %s",
                    row.name,
                )
                continue
            options.append(
                {
                    "toolName": row.name,
                    "description": row.description or "",
                    "inputSchema": dict(row.input_schema),
                }
            )
        return options

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _assertWithinSystemCeilings(
        self,
        dto: AgentOrchestrationProfileCreate | AgentOrchestrationProfileUpdate,
    ) -> None:
        """预算上限校验委托共享助手（编排运行共用同一份系统天花板）。"""
        assertWithinSystemCeilings(_profileLimits(dto), _PROFILE_LIMIT_LABELS)

    async def _assertNoProfile(
        self,
        session: AsyncSession,
        agentId: int,
        agentCode: str,
    ) -> None:
        existing = (
            await session.execute(
                select(AgentOrchestrationProfile.id).where(
                    AgentOrchestrationProfile.agent_id == agentId
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(f"Agent {agentCode} 已存在编排配置")

    async def _getProfile(
        self,
        session: AsyncSession,
        agentId: int,
        agentCode: str,
    ) -> AgentOrchestrationProfile:
        profile = (
            await session.execute(
                select(AgentOrchestrationProfile).where(
                    AgentOrchestrationProfile.agent_id == agentId
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise NotFoundError(f"Agent {agentCode} 尚无编排配置")
        return profile

    async def _loadBindableTools(
        self,
        session: AsyncSession,
        toolNames: list[str],
    ) -> list[AgentToolConfig]:
        """按请求顺序校验并返回可绑定的工具行。"""
        if not toolNames:
            return []
        rows = (
            (
                await session.execute(
                    select(AgentToolConfig).where(
                        AgentToolConfig.name.in_(toolNames)
                    )
                )
            )
            .scalars()
            .all()
        )
        byName = {row.name: row for row in rows}

        ordered: list[AgentToolConfig] = []
        for name in toolNames:
            row = byName.get(name)
            if row is None:
                raise ValidationError(f"工具不存在：{name}")
            assertOrchestrationToolValid(row, name)
            ordered.append(row)
        return ordered

    async def _loadBindings(
        self,
        session: AsyncSession,
        profileId: int,
    ) -> list[AgentOrchestrationBinding]:
        rows = (
            (
                await session.execute(
                    select(AgentOrchestrationBinding)
                    .where(AgentOrchestrationBinding.profile_id == profileId)
                    .order_by(
                        AgentOrchestrationBinding.sort_order,
                        AgentOrchestrationBinding.tool_name,
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _loadBindingsForProfiles(
        self,
        session: AsyncSession,
        profileIds: list[int],
    ) -> dict[int, list[AgentOrchestrationBinding]]:
        rows = (
            (
                await session.execute(
                    select(AgentOrchestrationBinding)
                    .where(AgentOrchestrationBinding.profile_id.in_(profileIds))
                    .order_by(
                        AgentOrchestrationBinding.profile_id,
                        AgentOrchestrationBinding.sort_order,
                        AgentOrchestrationBinding.tool_name,
                    )
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[int, list[AgentOrchestrationBinding]] = {}
        for row in rows:
            grouped.setdefault(row.profile_id, []).append(row)
        return grouped

    async def _enqueueAudit(
        self,
        session: AsyncSession,
        profile: AgentOrchestrationProfile,
        bindings: list[AgentOrchestrationBinding],
        actor: CurrentUser,
        *,
        eventAction: str,
        before: dict | None,
    ) -> None:
        await self._outbox.enqueue(
            session,
            event_type=f"{_AUDIT_EVENT_PREFIX}_{eventAction}",
            entity_type=_AUDIT_ENTITY_TYPE,
            entity_id=profile.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={
                "before": before,
                "after": _profileToAuditDict(profile, bindings),
            },
        )


def _dedupeToolNames(toolNames: list[str]) -> list[str]:
    """去重并保留首次出现顺序（新列表，不修改入参）。"""
    return list(dict.fromkeys(toolNames))


def _buildBindings(
    profileId: int,
    tools: list[AgentToolConfig],
) -> list[AgentOrchestrationBinding]:
    """按工具顺序构造全新的绑定对象（sort_order = 下标）。"""
    return [
        AgentOrchestrationBinding(
            profile_id=profileId,
            tool_name=tool.name,
            enabled=True,
            sort_order=index,
        )
        for index, tool in enumerate(tools)
    ]


def _assertRevision(current: int, expected: int) -> None:
    if current != expected:
        raise ConflictError(
            f"编排配置 revision 不匹配：current={current}, request={expected}"
        )
