"""AI 特征定义服务（Phase 4.3）。

承载 feature_definition 表 CRUD + 特征值列表查询。owner-based ACL（entity_mapping
同模式，无 audit/history）：owner 由 actor.departments[0] 派生，update/delete 走
AclService.assertCanModify；create 不接受 client body 声明 owner（DTO 无 owner 字段）。

calculation_logic 只读校验：创建/更新时经 _assert_read_only 校验（SqlSafetyError →
400），计算时 execute_read_only 二次校验，双重护栏防 SQL 注入。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataSource, FeatureDefinition, FeatureValue
from app.domain.schemas import FeatureDefinitionCreate, FeatureDefinitionUpdate
from app.infrastructure.business_db_pool import _assert_read_only
from app.services.acl_service import AclService
from app.services.messages_zh import (
    MSG_FEATURE_DATASOURCE_NOT_FOUND,
    MSG_FEATURE_DUPLICATE_NAME,
    MSG_FEATURE_NOT_FOUND,
)

# update 中不允许置 NULL 的列（None 语义为「不动」）；可空列允许置 None 清除。
_NON_NULL_UPDATE_FIELDS = frozenset(
    {
        "feature_name",
        "entity_type",
        "calculation_logic",
        "refresh_frequency",
        "version",
        "status",
        "is_enabled",
        "datasource_id",
    }
)


def _duplicateError(name: str) -> ConflictError:
    """唯一冲突错误：查重命中与并发 commit 失败共用同一消息。"""
    return ConflictError(MSG_FEATURE_DUPLICATE_NAME.format(name=name))


class FeatureDefinitionService:
    """AI 特征定义 CRUD + 特征值列表查询。"""

    def __init__(self, acl: AclService | None = None) -> None:
        # 默认实例：service 内部 new；测试可注入 mock
        self._acl = acl or AclService()

    async def listFeatures(self, session: AsyncSession) -> list[FeatureDefinition]:
        """按 feature_name 升序列出全部特征定义。"""
        stmt = select(FeatureDefinition).order_by(FeatureDefinition.feature_name)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getFeature(self, session: AsyncSession, id: int) -> FeatureDefinition:
        """按 id 取特征定义；不存在抛 NotFoundError。"""
        entity = await session.get(FeatureDefinition, id)
        if entity is None:
            raise NotFoundError(MSG_FEATURE_NOT_FOUND.format(id=id))
        return entity

    async def createFeature(
        self,
        session: AsyncSession,
        dto: FeatureDefinitionCreate,
        actor: CurrentUser,
    ) -> FeatureDefinition:
        """创建特征定义。

        - calculation_logic 先过 _assert_read_only（写操作 → SqlSafetyError 400）
        - datasource_id 存在性校验（不存在 → ValidationError 422）
        - feature_name 查重（重复 → ConflictError 409，DB 唯一索引兜底 race）
        - owner = actor.departments[0]（为空则 None → 仅 admin 可改）
        """
        _assert_read_only(dto.calculation_logic)
        await self._assertDatasourceExists(session, dto.datasource_id)

        existing = await session.execute(
            select(FeatureDefinition).where(
                FeatureDefinition.feature_name == dto.feature_name
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise _duplicateError(dto.feature_name)

        derivedOwner = actor.departments[0] if actor.departments else None
        entity = FeatureDefinition(
            feature_name=dto.feature_name,
            feature_alias=dto.feature_alias,
            feature_definition=dto.feature_definition,
            entity_type=dto.entity_type,
            calculation_logic=dto.calculation_logic,
            window_size=dto.window_size,
            refresh_frequency=dto.refresh_frequency,
            unit=dto.unit,
            version=dto.version or "v1.0",
            status=dto.status,
            is_enabled=dto.is_enabled,
            datasource_id=dto.datasource_id,
            created_by=actor.userId,
            owner=derivedOwner,
        )
        session.add(entity)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _duplicateError(dto.feature_name) from exc
        await session.refresh(entity)
        return entity

    async def updateFeature(
        self,
        session: AsyncSession,
        id: int,
        dto: FeatureDefinitionUpdate,
        actor: CurrentUser,
    ) -> FeatureDefinition:
        """局部更新特征定义；非空列 None 视为不动，可空列 None 清空。

        先 ACL 检查（owner 不匹配 + 非 admin → PermissionDeniedError），再校验
        calculation_logic 只读 / datasource 存在性，最后 apply + commit。
        """
        entity = await self.getFeature(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="FEATURE",
            entity_code=str(entity.id),
        )
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        if changes.get("calculation_logic") is not None:
            _assert_read_only(changes["calculation_logic"])
        if changes.get("datasource_id") is not None:
            await self._assertDatasourceExists(session, changes["datasource_id"])
        for field, value in changes.items():
            if value is None and field in _NON_NULL_UPDATE_FIELDS:
                continue
            setattr(entity, field, value)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _duplicateError(str(changes.get("feature_name", entity.feature_name))) from exc
        await session.refresh(entity)
        return entity

    async def deleteFeature(
        self,
        session: AsyncSession,
        id: int,
        actor: CurrentUser,
    ) -> None:
        """删除特征定义（级联删 feature_value，ondelete CASCADE）。

        先 ACL 检查（owner 不匹配 + 非 admin → 403）。
        """
        entity = await self.getFeature(session, id)
        self._acl.assertCanModify(
            actor,
            entity_owner=entity.owner,
            entity_label="FEATURE",
            entity_code=str(entity.id),
        )
        await session.delete(entity)
        await session.commit()

    async def listValues(
        self,
        session: AsyncSession,
        feature_id: int,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[FeatureValue]:
        """列出某特征的特征值（先校验特征存在），按 entity_key + valid_at 倒序分页。"""
        await self.getFeature(session, feature_id)
        stmt = (
            select(FeatureValue)
            .where(FeatureValue.feature_id == feature_id)
            .order_by(FeatureValue.entity_key, FeatureValue.valid_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _assertDatasourceExists(
        self, session: AsyncSession, datasource_id: int
    ) -> None:
        """校验 datasource_id 存在；不存在抛 ValidationError（→ 422）。"""
        ds = await session.get(DataSource, datasource_id)
        if ds is None:
            raise ValidationError(
                MSG_FEATURE_DATASOURCE_NOT_FOUND.format(id=datasource_id)
            )
