"""Agent Registry 服务（Phase 6.1）。

提供 AgentDefinition + AgentAccessPolicy 的 CRUD 与查询：
- list / get / create / update / soft-delete（status → deprecated）
- 按 status / data_domain 过滤
- 嵌套策略 CRUD（list / create / update / delete）

ACL（Phase 4.5 模式）：
- 读：所有登录用户（registry 透明度）
- 写：仅 admin（创建/更新/删除/策略 CRUD 统一入口都过 assertCanModify，
  owner 不存在时仅 admin 可改）
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dependencies import CurrentUser
from app.domain.enums import AgentStatus
from app.domain.error_messages import (
    MSG_AGENT_DUPLICATE_CODE,
    MSG_AGENT_NOT_FOUND_BY_CODE,
    MSG_AGENT_POLICY_DUPLICATE,
    MSG_AGENT_POLICY_NOT_FOUND,
    MSG_AGENT_REGISTRY_FORBIDDEN,
)
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.models import AgentAccessPolicy, AgentDefinition
from app.domain.schemas import (
    AgentAccessPolicyCreate,
    AgentAccessPolicyRead,
    AgentAccessPolicyUpdate,
    AgentDefinitionCreate,
    AgentDefinitionRead,
    AgentDefinitionUpdate,
)
from app.services.acl_service import AclService
from app.services.agent_binding_cache import agent_binding_cache
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

_audit = AuditService()


def _policyToRead(policy: AgentAccessPolicy) -> AgentAccessPolicyRead:
    return AgentAccessPolicyRead.model_validate(policy)


def agentToRead(entity: AgentDefinition) -> AgentDefinitionRead:
    """ORM → DTO 转换；填充 Phase 6.4 派生字段 ``runnable``。

    ``runnable`` = ``status==ACTIVE AND cache 有 tool_name 绑定``。
    若 cache 未 warmUp（lifespan bug），视为不可运行（try/except 兜底）。
    """
    read = AgentDefinitionRead.model_validate(entity)
    try:
        tool = agent_binding_cache.getToolName(read.agent_code)
    except RuntimeError:
        tool = None  # 未 warmUp 视为不可运行
    read.runnable = (
        read.status == AgentStatus.ACTIVE and tool is not None
    )
    return read


def _agentToDict(entity: AgentDefinition) -> dict:
    """将 AgentDefinition 模型实例转换为字典（审计用）。"""
    return {
        "id": entity.id,
        "agent_code": entity.agent_code,
        "agent_name": entity.agent_name,
        "description": entity.description,
        "trigger_type": entity.trigger_type.value if hasattr(entity.trigger_type, "value") else entity.trigger_type,
        "response_latency": entity.response_latency.value if hasattr(entity.response_latency, "value") else entity.response_latency,
        "data_domains": entity.data_domains,
        "data_layers": entity.data_layers,
        "status": entity.status.value if hasattr(entity.status, "value") else entity.status,
        "owner": entity.owner,
        "version": entity.version,
        "tool_name": entity.tool_name,
    }


class AgentRegistryService:
    """Agent 注册中心业务逻辑。"""

    def __init__(self, acl: AclService | None = None) -> None:
        self._acl = acl or AclService()

    async def listAgents(
        self,
        session: AsyncSession,
        *,
        status: AgentStatus | None = None,
        data_layer: str | None = None,
        data_domains: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AgentDefinition]:
        """按 agent_code 升序列表；支持 status / data_layer / data_domains 过滤。

        - data_layer（单值）：JSONB @> 包含
        - data_domains（多值）：JSONB && 重叠（OR 语义）
        """
        stmt = select(AgentDefinition).options(selectinload(AgentDefinition.policies))
        if status is not None:
            stmt = stmt.where(AgentDefinition.status == status)
        if data_layer is not None:
            # JSONB 数组字段：用 contains（@>）匹配子集
            stmt = stmt.where(AgentDefinition.data_layers.contains([data_layer]))
        if data_domains:
            # JSONB 数组字段：用 contains + OR 做 OR 过滤（任一相交即命中）
            or_conditions = [
                AgentDefinition.data_domains.contains([d]) for d in data_domains
            ]
            stmt = stmt.where(or_(*or_conditions))
        stmt = stmt.order_by(AgentDefinition.agent_code).limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getAgent(
        self,
        session: AsyncSession,
        agent_code: str,
    ) -> AgentDefinition:
        row = await session.execute(
            select(AgentDefinition)
            .options(selectinload(AgentDefinition.policies))
            .where(AgentDefinition.agent_code == agent_code)
        )
        entity = row.scalar_one_or_none()
        if entity is None:
            raise NotFoundError(MSG_AGENT_NOT_FOUND_BY_CODE.format(code=agent_code))
        return entity

    async def createAgent(
        self,
        session: AsyncSession,
        dto: AgentDefinitionCreate,
        actor: CurrentUser,
    ) -> AgentDefinition:
        """创建 Agent（含初始策略集）。owner 由 actor.departments[0] 派生。

        重复 agent_code 抛 ConflictError。CREATE 不做 ACL 检查（无既有 entity，
        无 owner 比较意义；任何部门都能注册新 Agent，但仅 admin 可后续修改）。
        """
        derived = actor.departments[0] if actor.departments else None

        entity = AgentDefinition(
            agent_code=dto.agent_code,
            agent_name=dto.agent_name,
            description=dto.description,
            trigger_type=dto.trigger_type.value,
            response_latency=dto.response_latency.value,
            data_domains=list(dto.data_domains),
            data_layers=list(dto.data_layers),
            status=dto.status.value,
            owner=derived,
            version=dto.version or "v1.0",
            tool_name=dto.tool_name,
        )
        # 内嵌策略：同时插入。重复 (data_object, data_layer) → IntegrityError → 409。
        for p in dto.policies:
            entity.policies.append(
                AgentAccessPolicy(
                    data_object=p.data_object,
                    permission=p.permission.value,
                    data_layer=p.data_layer,
                    notes=p.notes,
                )
            )
        session.add(entity)
        # flush 必须与 commit 同在 try 内：agent_code 重复在 flush 即抛
        # IntegrityError，包不到就是裸 500 而非 409
        try:
            await session.flush()
            await _audit.record(
                session,
                entity_type="agent_definition",
                entity_id=entity.id,
                action="CREATE",
                actor=actor.userId,
                actor_departments=actor.departments,
                after=_agentToDict(entity),
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_AGENT_DUPLICATE_CODE.format(code=dto.agent_code)
            ) from exc
        await session.refresh(entity, attribute_names=["policies"])
        # Task 6：写完立即刷新缓存（tool_name 在 commit 后已落库）
        await agent_binding_cache.refreshOne(session, entity.agent_code)
        logger.info(
            "注册 Agent code=%s policies=%d owner=%s",
            entity.agent_code,
            len(entity.policies),
            entity.owner,
        )
        return entity

    async def updateAgent(
        self,
        session: AsyncSession,
        agent_code: str,
        dto: AgentDefinitionUpdate,
        actor: CurrentUser,
    ) -> AgentDefinition:
        """局部更新 Agent 元数据；ACL 由 owner + admin 控制。"""
        entity = await self.getAgent(session, agent_code)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="AGENT_REGISTRY",
            entity_code=entity.agent_code,
        )
        before_state = _agentToDict(entity)
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            # enum 字段保持字符串值（ORM 列是 String）
            if field in ("trigger_type", "response_latency", "status") and value is not None:
                value = value.value if hasattr(value, "value") else value
            setattr(entity, field, value)
        await session.flush()
        await _audit.record(
            session,
            entity_type="agent_definition",
            entity_id=entity.id,
            action="UPDATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before_state,
            after=_agentToDict(entity),
        )
        await session.commit()
        await session.refresh(entity, attribute_names=["policies"])
        # Task 6：更新后刷新缓存（tool_name 可能已改）
        await agent_binding_cache.refreshOne(session, entity.agent_code)
        return entity

    async def deprecateAgent(
        self,
        session: AsyncSession,
        agent_code: str,
        actor: CurrentUser,
    ) -> AgentDefinition:
        """软删除（status → deprecated）。ACL 同 update。"""
        entity = await self.getAgent(session, agent_code)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="AGENT_REGISTRY",
            entity_code=entity.agent_code,
        )
        before_state = _agentToDict(entity)
        entity.status = AgentStatus.DEPRECATED.value
        await session.flush()
        await _audit.record(
            session,
            entity_type="agent_definition",
            entity_id=entity.id,
            action="UPDATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before_state,
            after=_agentToDict(entity),
        )
        await session.commit()
        await session.refresh(entity, attribute_names=["policies"])
        # Task 6：软删除后 invalidate cache（agent_code 不变，status 变）
        agent_binding_cache.invalidate(entity.agent_code)
        return entity

    async def deleteAgent(
        self,
        session: AsyncSession,
        agent_code: str,
        actor: CurrentUser,
    ) -> None:
        """硬删除 Agent（含级联策略）。ACL 同 update。"""
        entity = await self.getAgent(session, agent_code)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="AGENT_REGISTRY",
            entity_code=entity.agent_code,
        )
        before_state = _agentToDict(entity)
        code = entity.agent_code
        await session.delete(entity)
        await session.flush()
        await _audit.record(
            session,
            entity_type="agent_definition",
            entity_id=entity.id,
            action="DELETE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before_state,
        )
        await session.commit()
        # Task 6：删除后 invalidate（硬删不存在回填）
        agent_binding_cache.invalidate(code)

    # -------------------------------------------------------------------------
    # 访问策略（嵌套 CRUD）
    # -------------------------------------------------------------------------

    async def listPolicies(
        self,
        session: AsyncSession,
        agent_code: str,
    ) -> Sequence[AgentAccessPolicy]:
        """列出某 Agent 的全部访问策略（agent 不存在 → NotFoundError）。"""
        agent = await self.getAgent(session, agent_code)
        return list(agent.policies)

    async def addPolicy(
        self,
        session: AsyncSession,
        agent_code: str,
        dto: AgentAccessPolicyCreate,
        actor: CurrentUser,
    ) -> AgentAccessPolicy:
        """追加策略。ACL 同 update。"""
        agent = await self.getAgent(session, agent_code)
        self._acl.assertCanModify(
            actor,
            entity_owner=agent.owner,
            entity_label="AGENT_REGISTRY",
            entity_code=agent.agent_code,
        )
        policy = AgentAccessPolicy(
            agent_id=agent.id,
            data_object=dto.data_object,
            permission=dto.permission.value,
            data_layer=dto.data_layer,
            notes=dto.notes,
        )
        session.add(policy)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(MSG_AGENT_POLICY_DUPLICATE) from exc
        await session.refresh(policy)
        return policy

    async def updatePolicy(
        self,
        session: AsyncSession,
        agent_code: str,
        policy_id: int,
        dto: AgentAccessPolicyUpdate,
        actor: CurrentUser,
    ) -> AgentAccessPolicy:
        """更新策略（permission / notes 字段）。ACL 同 update。"""
        agent = await self.getAgent(session, agent_code)
        self._acl.assertCanModify(
            actor,
            entity_owner=agent.owner,
            entity_label="AGENT_REGISTRY",
            entity_code=agent.agent_code,
        )
        policy = await session.get(AgentAccessPolicy, policy_id)
        if policy is None or policy.agent_id != agent.id:
            raise NotFoundError(MSG_AGENT_POLICY_NOT_FOUND.format(id=policy_id))
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            if field == "permission" and value is not None:
                value = value.value if hasattr(value, "value") else value
            if value is None and field != "data_layer" and field != "notes":
                continue
            setattr(policy, field, value)
        await session.commit()
        await session.refresh(policy)
        return policy

    async def deletePolicy(
        self,
        session: AsyncSession,
        agent_code: str,
        policy_id: int,
        actor: CurrentUser,
    ) -> None:
        """删除策略。ACL 同 update。"""
        agent = await self.getAgent(session, agent_code)
        self._acl.assertCanModify(
            actor,
            entity_owner=agent.owner,
            entity_label="AGENT_REGISTRY",
            entity_code=agent.agent_code,
        )
        policy = await session.get(AgentAccessPolicy, policy_id)
        if policy is None or policy.agent_id != agent.id:
            raise NotFoundError(MSG_AGENT_POLICY_NOT_FOUND.format(id=policy_id))
        await session.delete(policy)
        await session.commit()