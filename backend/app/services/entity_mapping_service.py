"""跨系统编码映射服务（Phase 3.1）。

承载 EntityMapping 表的 CRUD：把各源系统（ERP/SRM/QMS/MDM/PLM）的原始编码
映射到企业统一代理键 / 统一编码。唯一约束 (entity_type, enterprise_key,
source_system) 由 service 层主动查重抛 ValidationError，DB 唯一索引兜底防 race。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EntityType, SourceSystem
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import EntityMapping
from app.domain.schemas import EntityMappingCreate, EntityMappingRead, EntityMappingUpdate
from app.services.messages_zh import (
    MSG_ENTITY_MAPPING_DATE_RANGE,
    MSG_ENTITY_MAPPING_EXISTS,
    MSG_ENTITY_MAPPING_NOT_FOUND,
)

# update 中不允许置 NULL 的列（None 语义为「不动」）；日期列允许置 None 以清除。
_NON_NULL_UPDATE_FIELDS = frozenset(
    {"enterprise_code", "source_key", "source_code", "match_rule"}
)


def _existsError(dto: EntityMappingCreate) -> ValidationError:
    """唯一冲突错误：查重命中与并发 commit 失败共用同一消息。"""
    return ValidationError(
        MSG_ENTITY_MAPPING_EXISTS.format(
            entityType=dto.entity_type.value,
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

    async def listMappings(
        self,
        session: AsyncSession,
        *,
        entityType: EntityType | None = None,
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

    async def createMapping(
        self,
        session: AsyncSession,
        dto: EntityMappingCreate,
    ) -> EntityMapping:
        """创建编码映射；同实体 + 同源系统重复抛 ValidationError。"""
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
        )
        session.add(entity)
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
    ) -> EntityMapping:
        """局部更新编码映射；非空列 None 视为不动，日期列可置 None 清除。"""
        entity = await self.getMapping(session, id)
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            if value is None and field in _NON_NULL_UPDATE_FIELDS:
                continue
            setattr(entity, field, value)
        _assertDateRange(effective_date=entity.effective_date, expiry_date=entity.expiry_date)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def deleteMapping(self, session: AsyncSession, id: int) -> None:
        """删除编码映射（物理删除，映射记录无历史追溯需求）。"""
        entity = await self.getMapping(session, id)
        await session.delete(entity)
        await session.commit()


def entityMappingToRead(mapping: EntityMapping) -> EntityMappingRead:
    """ORM -> Read DTO。集中导出便于路由层复用与单测覆盖。"""
    return EntityMappingRead.model_validate(mapping, from_attributes=True)
