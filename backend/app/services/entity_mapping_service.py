"""跨系统编码映射服务（Phase 3.1 + Phase 4.5 扩展 owner-based ACL）。

承载 EntityMapping 表的 CRUD：把各源系统（ERP/SRM/QMS/MDM/PLM）的原始编码
映射到企业统一代理键 / 统一编码。唯一约束 (entity_type, enterprise_key,
source_system) 由 service 层主动查重抛 ValidationError，DB 唯一索引兜底防 race。

Phase 4.5 扩展：updateMapping / deleteMapping 走 AclService.assertCanModify。
createMapping 接收 actor 并将 owner 设为 actor.departments[0]（防止 client
任意声明 owner 越权）；actor.departments 为空时 owner=None（仅 admin 可改）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import BusinessObjectCode, SourceSystem
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import EntityMapping
from app.domain.schemas import (
    EntityMappingCreate,
    EntityMappingRead,
    EntityMappingSearchHit,
    EntityMappingUpdate,
)
from app.services.acl_service import AclService
from app.services.messages_zh import (
    MSG_ENTITY_MAPPING_DATE_RANGE,
    MSG_ENTITY_MAPPING_EXISTS,
    MSG_ENTITY_MAPPING_NOT_FOUND,
)
from app.services.outbox_service import OutboxService

# update 中不允许置 NULL 的列（None 语义为「不动」）；日期列允许置 None 以清除。
_NON_NULL_UPDATE_FIELDS = frozenset(
    {"enterprise_code", "source_key", "source_code", "match_rule"}
)


def _entityToDict(entity: EntityMapping) -> dict:
    """EntityMapping 实体 → JSON 可序列化 dict（用于 outbox payload）。

    date / datetime 等非 JSON-native 类型在 dict 内原样保留会导致 PG JSONB 写入失败，
    故在此统一转字符串。
    """
    out: dict = {}
    for col in entity.__table__.columns.keys():
        v = getattr(entity, col)
        if hasattr(v, "value"):  # Enum
            out[col] = v.value
        elif isinstance(v, (date, datetime)):
            out[col] = v.isoformat()
        else:
            out[col] = v
    return out


def _existsError(dto: EntityMappingCreate) -> ValidationError:
    """唯一冲突错误：查重命中与并发 commit 失败共用同一消息。"""
    return ValidationError(
        MSG_ENTITY_MAPPING_EXISTS.format(
            entityType=dto.entity_type,
            enterpriseKey=dto.enterprise_key,
            sourceSystem=dto.source_system.value,
        )
    )


def _assertDateRange(*, effective_date: date | None, expiry_date: date | None) -> None:
    """生效日期不得晚于失效日期；仅当两端均非空时校验。"""
    if effective_date is not None and expiry_date is not None:
        if effective_date > expiry_date:
            raise ValidationError(
                MSG_ENTITY_MAPPING_DATE_RANGE.format(
                    effectiveDate=effective_date.isoformat(),
                    expiryDate=expiry_date.isoformat(),
                )
            )


class EntityMappingService:
    """跨系统编码映射 CRUD。"""

    def __init__(self, acl: AclService | None = None, outbox: OutboxService | None = None) -> None:
        # 默认实例：service 内部 new；测试可注入 mock
        self._acl = acl or AclService()
        self._outbox = outbox or OutboxService()

    async def listMappings(
        self,
        session: AsyncSession,
        *,
        entityType: BusinessObjectCode | None = None,
        sourceSystem: SourceSystem | None = None,
        enterpriseKey: int | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[EntityMapping]:
        """列表查询，可按 entityType / sourceSystem / enterpriseKey 过滤，分页返回。"""
        stmt = select(EntityMapping).order_by(EntityMapping.id)
        if entityType is not None:
            stmt = stmt.where(EntityMapping.entity_type == entityType)
        if sourceSystem is not None:
            stmt = stmt.where(EntityMapping.source_system == sourceSystem)
        if enterpriseKey is not None:
            stmt = stmt.where(EntityMapping.enterprise_key == enterpriseKey)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getMapping(self, session: AsyncSession, id: int) -> EntityMapping:
        """按 id 取映射；不存在抛 NotFoundError。"""
        entity = await session.get(EntityMapping, id)
        if entity is None:
            raise NotFoundError(MSG_ENTITY_MAPPING_NOT_FOUND.format(id=id))
        return entity

    async def searchMappings(
        self,
        session: AsyncSession,
        *,
        q: str,
        entityType: BusinessObjectCode | None = None,
        limit: int = 20,
    ) -> list[EntityMapping]:
        """模糊搜索编码映射（Phase 6.x AutoComplete 用）。

        - `q` 空字符串 → 返回空列表（避免无过滤返回全表 + 与前端空查询语义一致）
        - `q` 全数字 → 同时按 `enterprise_key` 精确匹配；否则按 `enterprise_code` /
          `source_code` ILIKE `%q%` 模糊匹配
        - `entityType` 过滤可选
        - 结果先按 enterprise_key 命中精确排序，再按 id 升序
        - `limit` 上限 100（防止误调拉全表）
        """
        q = (q or "").strip()
        if not q:
            return []
        limit = max(1, min(limit, 100))
        stmt = select(EntityMapping)
        conds = []
        if q.isdigit():
            conds.append(EntityMapping.enterprise_key == int(q))
        # 始终加 ILIKE 兜底，让"输错数字也能搜到含此串的 enterprise_code"
        like = f"%{q}%"
        conds.append(EntityMapping.enterprise_code.ilike(like))
        conds.append(EntityMapping.source_code.ilike(like))
        stmt = stmt.where(or_(*conds))
        if entityType is not None:
            stmt = stmt.where(EntityMapping.entity_type == entityType)
        # 排序：精确 enterprise_key 命中排前（按 q 全数字），其余按 id 稳定
        if q.isdigit():
            stmt = stmt.order_by(
                (EntityMapping.enterprise_key == int(q)).desc(),
                EntityMapping.id,
            )
        else:
            stmt = stmt.order_by(EntityMapping.id)
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def createMapping(
        self,
        session: AsyncSession,
        dto: EntityMappingCreate,
        actor: CurrentUser,
    ) -> EntityMapping:
        """创建编码映射；同实体 + 同源系统重复抛 ValidationError。

        Phase 4.5：owner 由 actor.departments[0] 派生，**不接受** client body
        中的 owner（已在 DTO 中移除），防止「finance 用户创建 owner=procurement
        的实体」式越权。actor.departments 为空 → owner=None → 仅 admin 可改。
        """
        _assertDateRange(effective_date=dto.effective_date, expiry_date=dto.expiry_date)
        # 唯一性查重（service 层兜底；三列均非空，DB 唯一索引也完整兜底）
        existing = await session.execute(
            select(EntityMapping).where(
                EntityMapping.entity_type == dto.entity_type,
                EntityMapping.enterprise_key == dto.enterprise_key,
                EntityMapping.source_system == dto.source_system,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise _existsError(dto)

        derivedOwner = actor.departments[0] if actor.departments else None
        entity = EntityMapping(
            entity_type=dto.entity_type,
            enterprise_key=dto.enterprise_key,
            enterprise_code=dto.enterprise_code,
            source_system=dto.source_system,
            source_key=dto.source_key,
            source_code=dto.source_code,
            match_rule=dto.match_rule,
            effective_date=dto.effective_date,
            expiry_date=dto.expiry_date,
            owner=derivedOwner,
        )
        session.add(entity)
        await session.flush()  # get entity.id for outbox payload
        # outbox 入队（同一事务绑定）：worker 消费后写 audit_log
        await self._outbox.enqueue(
            session,
            event_type="entity_mapping_created",
            entity_type="entity_mapping",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"after": _entityToDict(entity)},
        )
        # 并发场景：两条请求同时越过查重，败者 commit 撞唯一索引 → 转 422 而非裸 500。
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _existsError(dto) from exc
        await session.refresh(entity)
        return entity

    async def updateMapping(
        self,
        session: AsyncSession,
        id: int,
        dto: EntityMappingUpdate,
        actor: CurrentUser,
    ) -> EntityMapping:
        """局部更新编码映射；非空列 None 视为不动，日期列可置 None 清除。

        Phase 4.5 扩展：先 ACL 检查（owner 不匹配 + 非 admin → PermissionDeniedError），
        再 apply dto changes + commit。
        """
        entity = await self.getMapping(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="ENTITY_MAPPING",
            entity_code=str(entity.id),
        )
        before = _entityToDict(entity)
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            if value is None and field in _NON_NULL_UPDATE_FIELDS:
                continue
            setattr(entity, field, value)
        _assertDateRange(effective_date=entity.effective_date, expiry_date=entity.expiry_date)
        # outbox 入队（同一事务绑定）：worker 消费后写 audit_log
        await self._outbox.enqueue(
            session,
            event_type="entity_mapping_updated",
            entity_type="entity_mapping",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"before": before, "after": _entityToDict(entity)},
        )
        await session.commit()
        await session.refresh(entity)
        return entity

    async def deleteMapping(
        self,
        session: AsyncSession,
        id: int,
        actor: CurrentUser,
    ) -> None:
        """删除编码映射（物理删除，映射记录无历史追溯需求）。

        Phase 4.5 扩展：先 ACL 检查（owner 不匹配 + 非 admin → 403）。
        """
        entity = await self.getMapping(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="ENTITY_MAPPING",
            entity_code=str(entity.id),
        )
        before = _entityToDict(entity)
        # outbox 入队先于 session.delete（保持 entity 属性可访问），同事务 commit
        await self._outbox.enqueue(
            session,
            event_type="entity_mapping_deleted",
            entity_type="entity_mapping",
            entity_id=entity.id,
            actor=actor.userId,
            actor_departments=actor.departments,
            payload={"before": before},
        )
        await session.delete(entity)
        await session.commit()


def entityMappingToRead(mapping: EntityMapping) -> EntityMappingRead:
    """ORM -> Read DTO。集中导出便于路由层复用与单测覆盖。"""
    return EntityMappingRead.model_validate(mapping, from_attributes=True)


def entityMappingSearchToHit(mapping: EntityMapping) -> EntityMappingSearchHit:
    """ORM → 搜索结果轻量 DTO（Phase 6.x AutoComplete 用）。

    与 entityMappingToRead 并列放在类外：searchMappings 返回 ORM 列表，
    路由层统一转 DTO（与既有 listMappings 模式一致）。
    """
    return EntityMappingSearchHit.model_validate(mapping, from_attributes=True)
