"""system_config 业务层（feat-system-config-admin）。

薄包装：对 `system_config` 表的 list / get / updateValue + audit_log 写入。

设计要点：
- 全局开关集中地（如 `ENABLE_L4_AGENT_LOOP` 决定 chat_service 是否走 L4 LangGraph
  agent loop）。改一行 → 影响下游路由，因此 UPDATE 必须写 audit_log 留痕。
- 没有 DELETE/CREATE：表行由 alembic 迁移 + 种子脚本管理（SSOT）；admin UI 仅编辑
  value。意图：避免「某个开关突然没了」导致运行时 silent fall-through。
- value 字段允许 null/空串（业务可表达"关闭/未配置"）。但 key/description 不可改。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import NotFoundError
from app.domain.models import SystemConfig
from app.services.audit_service import AuditService

_audit = AuditService()


class SystemConfigService:
    """system_config 读写 + audit log。"""

    async def listAll(self, session: AsyncSession) -> list[SystemConfig]:
        """全量列表（admin UI 用）。按 key 升序保证 UI 稳定。

        注意：行很少（< 100 行种子级），全量无分页必要。
        """
        stmt = select(SystemConfig).order_by(SystemConfig.key.asc())
        return list((await session.execute(stmt)).scalars().all())

    async def getByKey(self, session: AsyncSession, key: str) -> SystemConfig:
        """按 key 查询；不存在抛 NotFoundError(404)。"""
        entity = await session.get(SystemConfig, key)
        if entity is None:
            raise NotFoundError(f"system_config 行不存在: key={key!r}")
        return entity

    async def updateValue(
        self,
        session: AsyncSession,
        key: str,
        value: str | None,
        *,
        actor: CurrentUser,
    ) -> SystemConfig:
        """更新 value 字段；同时写 audit_log（before/after 双端）。

        - 校验 key 存在（NotFoundError）。
        - 即使 value 没变也走 audit（admin 操作留痕优于"无变化跳过"）。
        - caller 负责 commit（与 chat_service / document_service 一致）。
        """
        entity = await self.getByKey(session, key)
        before = {"value": entity.value}
        entity.value = value
        # DB 仅 INSERT 侧有 DEFAULT NOW（无 trigger 自动维护 updated_time）；
        # UPDATE 必须显式 set，否则前端读到的还是旧时间。
        entity.updated_time = datetime.now(timezone.utc)
        await _audit.record(
            session,
            entity_type="system_config",
            entity_id=0,  # VARCHAR PK 不适合 entity_id=int，用 0 占位
            action="UPDATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before,
            after={"value": entity.value},
        )
        return entity