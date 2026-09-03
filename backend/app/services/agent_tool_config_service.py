"""Agent 工具配置 CRUD + outbox 审计（feat-agent-tool-config-db, 2026-09-03）。

DB-backed SSOT：业务人员通过 /admin/tools UI CRUD；handler 引擎在代码。
所有写操作通过 OutboxService.enqueue 写审计，caller commit（同事务原子性）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import AgentToolHandlerKind
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AgentDefinition, AgentToolConfig
from app.domain.schemas import (
    AgentToolConfigCreate,
    AgentToolConfigUpdate,
    _UnsetType,
    _normalizeDataObject,
)
from app.services.agent_tools import _VALID_HANDLER_REFS
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)


def _configRowToDict(row: AgentToolConfig) -> dict:
    """序列化 ORM 为 JSON-safe dict（含 datetime/JSONB）。"""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "data_object": row.data_object,
        "data_layers": list(row.data_layers or []),
        "input_schema": row.input_schema or {},
        "handler_kind": row.handler_kind,
        "handler_ref": row.handler_ref,
        "arg_extractor_kind": row.arg_extractor_kind,
        "enabled": row.enabled,
        "version": row.version,
        "created_time": row.created_time.isoformat() if row.created_time else None,
        "updated_time": row.updated_time.isoformat() if row.updated_time else None,
    }


class AgentToolConfigService:
    def __init__(
        self,
        acl=None,
        outbox: OutboxService | None = None,
    ) -> None:
        self._acl = acl
        self._outbox = outbox or OutboxService()

    # ---- read ----

    async def listTools(
        self, session: AsyncSession, *, enabledOnly: bool = False
    ) -> list[AgentToolConfig]:
        stmt = select(AgentToolConfig)
        if enabledOnly:
            stmt = stmt.where(AgentToolConfig.enabled.is_(True))
        rows = (await session.execute(stmt)).scalars().all()
        return list(rows)

    async def getTool(self, session: AsyncSession, name: str) -> AgentToolConfig:
        row = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == name)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"agent_tool_config not found: {name}")
        return row

    # ---- write ----

    async def createTool(
        self,
        session: AsyncSession,
        dto: AgentToolConfigCreate,
        actor: CurrentUser,
    ) -> AgentToolConfig:
        # 校验 handler_kind/ref combo（service 层兜底；ORM CHECK 仅防 SQL 层越界）
        kind_value = dto.handler_kind.value
        if dto.handler_ref not in _VALID_HANDLER_REFS.get(kind_value, frozenset()):
            raise ValidationError(
                f"handler_ref {dto.handler_ref!r} 不在 {kind_value} 白名单"
            )

        row = AgentToolConfig(
            name=dto.name,
            description=dto.description,
            data_object=_normalizeDataObject(dto.data_object),
            data_layers=list(dto.data_layers or []),
            input_schema=dto.input_schema or {},
            handler_kind=kind_value,
            handler_ref=dto.handler_ref,
            arg_extractor_kind=dto.arg_extractor_kind,
            enabled=True,
            version=1,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            if "uq_agent_tool_config_name" in str(e.orig):
                raise ConflictError(f"agent_tool_config name 已存在: {dto.name}")
            raise

        await self._outbox.enqueue(
            session,
            event_type="agent_tool_created",
            entity_type="agent_tool_config",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": None, "after": _configRowToDict(row)},
        )
        return row

    async def updateTool(
        self,
        session: AsyncSession,
        name: str,
        dto: AgentToolConfigUpdate,
        actor: CurrentUser,
    ) -> AgentToolConfig:
        row = await self.getTool(session, name)
        before = _configRowToDict(row)
        if row.version != dto.version:
            raise ConflictError(
                f"agent_tool_config version mismatch: current={row.version}, dto={dto.version}"
            )

        # 应用 UnsetType 字段（区分「未提供」vs「显式赋值」）
        if not isinstance(dto.description, _UnsetType):
            row.description = dto.description
        if not isinstance(dto.data_object, _UnsetType):
            row.data_object = _normalizeDataObject(dto.data_object)
        if not isinstance(dto.data_layers, _UnsetType):
            row.data_layers = list(dto.data_layers or [])
        if not isinstance(dto.input_schema, _UnsetType):
            row.input_schema = dto.input_schema or {}
        if not isinstance(dto.handler_kind, _UnsetType):
            new_kind = dto.handler_kind.value
            # 若 handler_kind 变更，必须保证 handler_ref 仍在新 kind 白名单
            if (
                new_kind != row.handler_kind
                and row.handler_ref not in _VALID_HANDLER_REFS.get(new_kind, frozenset())
            ):
                raise ValidationError(
                    f"handler_kind 改为 {new_kind} 时 handler_ref {row.handler_ref!r} 不在新白名单"
                )
            row.handler_kind = new_kind
        if not isinstance(dto.handler_ref, _UnsetType):
            if dto.handler_ref not in _VALID_HANDLER_REFS.get(row.handler_kind, frozenset()):
                raise ValidationError(
                    f"handler_ref {dto.handler_ref!r} 不在 {row.handler_kind} 白名单"
                )
            row.handler_ref = dto.handler_ref
        if not isinstance(dto.arg_extractor_kind, _UnsetType):
            row.arg_extractor_kind = dto.arg_extractor_kind
        if not isinstance(dto.enabled, _UnsetType):
            row.enabled = dto.enabled

        row.version += 1
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="agent_tool_updated",
            entity_type="agent_tool_config",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": _configRowToDict(row)},
        )
        return row

    async def deleteTool(
        self,
        session: AsyncSession,
        name: str,
        actor: CurrentUser,
    ) -> None:
        row = await self.getTool(session, name)

        # 检查 AgentDefinition.tool_name 是否引用此工具
        referencing = (
            await session.execute(
                select(AgentDefinition.agent_code).where(
                    AgentDefinition.tool_name == name
                )
            )
        ).scalars().all()
        if referencing:
            raise _ToolInUseConflict(name, list(referencing))

        before = _configRowToDict(row)
        deleted_id = row.id
        await session.delete(row)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="agent_tool_deleted",
            entity_type="agent_tool_config",
            entity_id=deleted_id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": None},
        )

    async def toggleEnabled(
        self,
        session: AsyncSession,
        name: str,
        enabled: bool,
        actor: CurrentUser,
    ) -> AgentToolConfig:
        row = await self.getTool(session, name)
        before = _configRowToDict(row)
        row.enabled = enabled
        row.version += 1
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="agent_tool_toggled",
            entity_type="agent_tool_config",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": _configRowToDict(row)},
        )
        return row

    # ---- seed ----

    async def upsertSeed(
        self,
        session: AsyncSession,
        name: str,
        fields: dict,
    ) -> AgentToolConfig:
        """Seed 专用 upsert（绕过 ACL）。name 命中 → 全量更新元数据；未命中 → 插入。"""
        existing = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == name)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = AgentToolConfig(
                name=name,
                description=fields.get("description"),
                data_object=_normalizeDataObject(fields["data_object"]),
                data_layers=list(fields.get("data_layers", [])),
                input_schema=fields.get("input_schema", {}),
                handler_kind=fields["handler_kind"],
                handler_ref=fields["handler_ref"],
                arg_extractor_kind=fields.get("arg_extractor_kind", "supplier_key"),
                enabled=fields.get("enabled", True),
                version=1,
            )
            session.add(row)
            await session.flush()
            return row

        existing.description = fields.get("description", existing.description)
        existing.data_object = _normalizeDataObject(fields["data_object"])
        existing.data_layers = list(fields.get("data_layers", []))
        existing.input_schema = fields.get("input_schema", {})
        existing.handler_kind = fields["handler_kind"]
        existing.handler_ref = fields["handler_ref"]
        existing.arg_extractor_kind = fields.get(
            "arg_extractor_kind", existing.arg_extractor_kind
        )
        existing.updated_time = datetime.now(timezone.utc)
        await session.flush()
        return existing


class _ToolInUseConflict(ConflictError):
    """删除被引用的 agent_tool_config 时抛出，detail 含 referencingAgents 列表。"""

    def __init__(self, name: str, referencing: list[str]) -> None:
        super().__init__(
            f"agent_tool_config 被 Agent 引用，无法删除: {name}",
            detail=f"referencingAgents={referencing}",
        )
        self.referencing_agents = referencing
        self.tool_name = name