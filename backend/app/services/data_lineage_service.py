"""数据血缘服务（Phase 2.1）。

仅承载 DataLineage 表的 CRUD；自动提取由 Phase 2.2 lineage_extractor + 一次性脚本
lineage_auto_extract.py 负责，本服务不耦合 ontology 解析路径。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LineageLayer
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataLineage
from app.domain.schemas import (
    LineageEdgeCreate,
    LineageEdgeRead,
    LineageEdgeUpdate,
)
from app.services.messages_zh import (
    MSG_LINEAGE_EDGE_EXISTS,
    MSG_LINEAGE_EDGE_NOT_FOUND,
    MSG_LINEAGE_SELF_LOOP,
)


def _edgeIdentityKey(
    *,
    source_layer: LineageLayer,
    source_system: str,
    source_object: str,
    source_field: str | None,
    target_layer: LineageLayer,
    target_system: str,
    target_object: str,
    target_field: str | None,
) -> tuple:
    """血缘边身份键（用于查重 + 唯一约束断言）。"""
    return (
        source_layer,
        source_system,
        source_object,
        source_field,
        target_layer,
        target_system,
        target_object,
        target_field,
    )


def _edgeLabel(
    layer: LineageLayer, system: str, obj: str, field: str | None
) -> str:
    """血缘边标签（用于错误消息）。"""
    base = f"{layer.value}.{system}.{obj}"
    return f"{base}.{field}" if field else base


class DataLineageService:
    """数据血缘 CRUD。"""

    async def listEdges(
        self,
        session: AsyncSession,
        *,
        sourceLayer: LineageLayer | None = None,
        targetLayer: LineageLayer | None = None,
        activeOnly: bool | None = None,
    ) -> list[DataLineage]:
        """列表查询，可按 sourceLayer / targetLayer / activeOnly 过滤。"""
        stmt = select(DataLineage).order_by(DataLineage.id)
        if sourceLayer is not None:
            stmt = stmt.where(DataLineage.source_layer == sourceLayer)
        if targetLayer is not None:
            stmt = stmt.where(DataLineage.target_layer == targetLayer)
        if activeOnly:
            stmt = stmt.where(DataLineage.is_active.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getEdge(self, session: AsyncSession, id: int) -> DataLineage:
        """按 id 取边；不存在抛 NotFoundError。"""
        entity = await session.get(DataLineage, id)
        if entity is None:
            raise NotFoundError(MSG_LINEAGE_EDGE_NOT_FOUND.format(id=id))
        return entity

    async def createEdge(
        self,
        session: AsyncSession,
        dto: LineageEdgeCreate,
    ) -> DataLineage:
        """创建血缘边；唯一冲突抛 ValidationError；自指抛 ValidationError。"""
        # 1. 自指校验：同层同对象循环
        if (
            dto.source_layer == dto.target_layer
            and dto.source_system == dto.target_system
            and dto.source_object == dto.target_object
            and dto.source_field == dto.target_field
        ):
            raise ValidationError(
                MSG_LINEAGE_SELF_LOOP.format(
                    src=_edgeLabel(
                        dto.source_layer,
                        dto.source_system,
                        dto.source_object,
                        dto.source_field,
                    ),
                    tgt=_edgeLabel(
                        dto.target_layer,
                        dto.target_system,
                        dto.target_object,
                        dto.target_field,
                    ),
                )
            )

        # 2. 唯一性查重（service 层兜底；PG NULL 不冲突，所以必须主动查）
        identity = _edgeIdentityKey(
            source_layer=dto.source_layer,
            source_system=dto.source_system,
            source_object=dto.source_object,
            source_field=dto.source_field,
            target_layer=dto.target_layer,
            target_system=dto.target_system,
            target_object=dto.target_object,
            target_field=dto.target_field,
        )
        existing = await session.execute(
            select(DataLineage).where(
                DataLineage.source_layer == identity[0],
                DataLineage.source_system == identity[1],
                DataLineage.source_object == identity[2],
                DataLineage.source_field.is_(identity[3])
                if identity[3] is None
                else DataLineage.source_field == identity[3],
                DataLineage.target_layer == identity[4],
                DataLineage.target_system == identity[5],
                DataLineage.target_object == identity[6],
                DataLineage.target_field.is_(identity[7])
                if identity[7] is None
                else DataLineage.target_field == identity[7],
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(
                MSG_LINEAGE_EDGE_EXISTS.format(
                    src=_edgeLabel(
                        dto.source_layer,
                        dto.source_system,
                        dto.source_object,
                        dto.source_field,
                    ),
                    tgt=_edgeLabel(
                        dto.target_layer,
                        dto.target_system,
                        dto.target_object,
                        dto.target_field,
                    ),
                )
            )

        entity = DataLineage(
            source_layer=dto.source_layer,
            source_system=dto.source_system,
            source_object=dto.source_object,
            source_field=dto.source_field,
            target_layer=dto.target_layer,
            target_system=dto.target_system,
            target_object=dto.target_object,
            target_field=dto.target_field,
            transformation_rule=dto.transformation_rule,
            refresh_frequency=dto.refresh_frequency,
            owner=dto.owner,
            description=dto.description,
            is_active=True,
        )
        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def updateEdge(
        self,
        session: AsyncSession,
        id: int,
        dto: LineageEdgeUpdate,
    ) -> DataLineage:
        """局部更新血缘边；只覆盖非 None 字段。"""
        entity = await self.getEdge(session, id)
        changes = dto.model_dump(exclude_unset=True, by_alias=False)
        for field, value in changes.items():
            setattr(entity, field, value)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def disableEdge(self, session: AsyncSession, id: int) -> None:
        """软删除：is_active=false（保留历史可视化追溯与审计）。"""
        entity = await self.getEdge(session, id)
        entity.is_active = False
        await session.commit()


def lineageToRead(edge: DataLineage) -> LineageEdgeRead:
    """ORM -> Read DTO。集中导出便于路由层复用与单测覆盖。"""
    return LineageEdgeRead.model_validate(edge, from_attributes=True)