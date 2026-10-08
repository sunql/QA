"""M0-P0.1 统一 ID 映射服务。

提供：
  register()    — 注册新映射，返回 unified_id
  resolve()     — 按 unified_id 查询
  resolveByExternal() — 按 (business_object, external_id) 查询
  update()      — 更新三库 ID（pg/neo4j/milvus）
  delete()      — 删除映射
  reconcile()   — 对账：返回三库 ID 不一致的记录

统一 ID 格式：obj:{business_object}:{external_id}
  business_object：如 supplier / customer / material
  external_id：业务侧原始编码

不负责三库实际写入（M0-P0.2 Neo4j / P0.3 Milvus 各自主导），
仅记录映射关系供查询。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import IdMapping
from app.domain.schemas import (
    IdMappingCreate,
    IdMappingRead,
    IdMappingUpdate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNIFIED_ID_TEMPLATE = "obj:{bo}:{ext}"


def _make_unified_id(business_object: str, external_id: str) -> str:
    return _UNIFIED_ID_TEMPLATE.format(bo=business_object, ext=external_id)


def _to_read(mapping: IdMapping) -> IdMappingRead:
    return IdMappingRead(
        unified_id=mapping.unified_id,
        business_object=mapping.business_object,
        external_id=mapping.external_id,
        pg_table=mapping.pg_table,
        pg_id=mapping.pg_id,
        neo4j_node_id=mapping.neo4j_node_id,
        milvus_collection=mapping.milvus_collection,
        milvus_id=mapping.milvus_id,
        created_time=mapping.created_time,
        updated_time=mapping.updated_time,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class IdMappingService:
    """统一 ID 映射 CRUD。"""

    async def register(
        self, session: AsyncSession, dto: IdMappingCreate
    ) -> IdMappingRead:
        """注册新映射。

        unified_id 自动生成：(business_object, external_id) 重复 → 409。
        """
        unified_id = _make_unified_id(dto.business_object, dto.external_id)
        mapping = IdMapping(
            unified_id=unified_id,
            business_object=dto.business_object,
            external_id=dto.external_id,
            pg_table=dto.pg_table,
            pg_id=dto.pg_id,
        )
        session.add(mapping)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise ConflictError(
                f"id_mapping 已存在：business_object={dto.business_object}, "
                f"external_id={dto.external_id}"
            )
        await session.commit()
        await session.refresh(mapping)
        return _to_read(mapping)

    async def resolve(
        self, session: AsyncSession, unified_id: str
    ) -> IdMappingRead:
        """按 unified_id 查询。"""
        row = await session.get(IdMapping, unified_id)
        if row is None:
            raise NotFoundError(f"id_mapping not found: {unified_id}")
        return _to_read(row)

    async def resolveByExternal(
        self, session: AsyncSession, business_object: str, external_id: str
    ) -> IdMappingRead | None:
        """按 (business_object, external_id) 查询，找不到返回 None。"""
        row = await session.execute(
            select(IdMapping).where(
                IdMapping.business_object == business_object,
                IdMapping.external_id == external_id,
            )
        )
        mapping = row.scalar_one_or_none()
        return _to_read(mapping) if mapping else None

    async def mapExternalIds(
        self, session: AsyncSession, business_object: str
    ) -> dict[str, str]:
        """一次性取 `{external_id: unified_id}`（批量解析，避免 N 次往返）。

        给「全量入图」这类需要把成批 PG id 转 unified_id 的调用方用：
        `listByBusinessObject` 带 200 条分页上限，全量场景（属性 3600+）会静默截断。
        """
        rows = await session.execute(
            select(IdMapping.external_id, IdMapping.unified_id).where(
                IdMapping.business_object == business_object
            )
        )
        return {externalId: unifiedId for externalId, unifiedId in rows.all()}

    async def listByBusinessObject(
        self, session: AsyncSession, business_object: str, limit: int = 200, offset: int = 0
    ) -> tuple[list[IdMappingRead], int]:
        """按业务对象列出映射。"""
        base_q = select(IdMapping).where(IdMapping.business_object == business_object)
        count_q = select(IdMapping.unified_id).where(IdMapping.business_object == business_object)

        total = len((await session.execute(count_q)).scalars().all())
        rows = await session.execute(
            base_q.order_by(IdMapping.unified_id).limit(limit).offset(offset)
        )
        mappings = rows.scalars().all()
        return [_to_read(m) for m in mappings], total

    async def update(
        self, session: AsyncSession, unified_id: str, dto: IdMappingUpdate
    ) -> IdMappingRead:
        """更新三库 ID 字段（pg / neo4j / milvus）。"""
        mapping = await session.get(IdMapping, unified_id)
        if mapping is None:
            raise NotFoundError(f"id_mapping not found: {unified_id}")

        if dto.pg_table is not None:
            mapping.pg_table = dto.pg_table
        if dto.pg_id is not None:
            mapping.pg_id = dto.pg_id
        if dto.neo4j_node_id is not None:
            mapping.neo4j_node_id = dto.neo4j_node_id
        if dto.milvus_collection is not None:
            mapping.milvus_collection = dto.milvus_collection
        if dto.milvus_id is not None:
            mapping.milvus_id = dto.milvus_id

        await session.flush()
        await session.commit()
        return _to_read(mapping)

    async def delete(self, session: AsyncSession, unified_id: str) -> None:
        """删除映射。"""
        mapping = await session.get(IdMapping, unified_id)
        if mapping is None:
            raise NotFoundError(f"id_mapping not found: {unified_id}")
        await session.delete(mapping)
        await session.flush()
        await session.commit()

    async def reconcile(self, session: AsyncSession) -> list[IdMappingRead]:
        """返回三库 ID 不一致的记录（至少一列为空）。"""
        rows = await session.execute(
            select(IdMapping).where(
                IdMapping.unified_id.isnot(None),
            )
        )
        all_mappings = rows.scalars().all()
        inconsistent = [
            m for m in all_mappings
            if not (m.pg_id and m.neo4j_node_id and m.milvus_id)
        ]
        return [_to_read(m) for m in inconsistent]
