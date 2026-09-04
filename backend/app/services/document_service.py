"""文档目录服务（Phase 5.1）。

DocumentCatalog + DocumentEntityRelation 的 CRUD。
document_id 业务唯一；storage_url / content_hash 可选（文件上传后续接入）。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import BusinessObjectCode, DocumentStatus
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DocumentCatalog, DocumentEntityRelation
from app.domain.schemas import (
    DocEntityRelationCreate,
    DocEntityRelationRead,
    DocumentCreate,
    DocumentRead,
    DocumentUpdate,
)
from app.services.audit_service import AuditService
from app.services.messages_zh import (
    MSG_DOCUMENT_DUPLICATE,
    MSG_DOCUMENT_NOT_FOUND,
    MSG_DOCUMENT_REL_EXISTS,
    MSG_DOCUMENT_REL_NOT_FOUND,
)

_audit = AuditService()

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000


def _duplicateError(document_id: str) -> ConflictError:
    return ConflictError(MSG_DOCUMENT_DUPLICATE.format(document_id=document_id))


def _entity_to_dict(entity: DocumentCatalog) -> dict:
    """Serialize DocumentCatalog ORM object for audit before/after snapshots."""
    return {
        "id": entity.id,
        "document_id": entity.document_id,
        "document_name": entity.document_name,
        "document_type": entity.document_type.value if hasattr(entity.document_type, "value") else entity.document_type,
        "version": entity.version,
        "status": entity.status.value if hasattr(entity.status, "value") else entity.status,
        "owner": entity.owner,
        "effective_date": str(entity.effective_date) if entity.effective_date else None,
        "security_level": entity.security_level.value if hasattr(entity.security_level, "value") else entity.security_level,
        "storage_url": entity.storage_url,
        "content_hash": entity.content_hash,
    }


class DocumentService:
    """文档目录 CRUD。"""

    async def listDocuments(
        self,
        session: AsyncSession,
        *,
        document_type: str | None = None,
        security_level: str | None = None,
        status: DocumentStatus | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[DocumentCatalog]:
        """列表查询，支持按类型/安全等级/状态过滤。"""
        limit = min(limit, _MAX_LIMIT)
        stmt = select(DocumentCatalog).order_by(DocumentCatalog.id)
        if document_type is not None:
            stmt = stmt.where(DocumentCatalog.document_type == document_type)
        if security_level is not None:
            stmt = stmt.where(DocumentCatalog.security_level == security_level)
        if status is not None:
            stmt = stmt.where(DocumentCatalog.status == status)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getDocument(
        self, session: AsyncSession, id: int
    ) -> DocumentCatalog:
        entity = await session.get(DocumentCatalog, id)
        if entity is None:
            raise NotFoundError(MSG_DOCUMENT_NOT_FOUND.format(id=id))
        return entity

    async def createDocument(
        self,
        session: AsyncSession,
        dto: DocumentCreate,
        actor: CurrentUser,
    ) -> DocumentCatalog:
        """创建文档；重复 document_id 抛 ConflictError（DB 唯一索引兜底 race）。"""
        existing = await session.execute(
            select(DocumentCatalog).where(
                DocumentCatalog.document_id == dto.document_id
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise _duplicateError(dto.document_id)
        entity = DocumentCatalog(
            document_id=dto.document_id,
            document_name=dto.document_name,
            document_type=dto.document_type,
            version=dto.version,
            status=DocumentStatus.ACTIVE,
            owner=dto.owner,
            effective_date=dto.effective_date,
            security_level=dto.security_level,
            storage_url=dto.storage_url,
            content_hash=dto.content_hash,
        )
        session.add(entity)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise _duplicateError(dto.document_id) from exc
        await _audit.record(
            session,
            entity_type="document_catalog",
            entity_id=entity.id,
            action="CREATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            after=_entity_to_dict(entity),
        )
        await session.commit()
        await session.refresh(entity)
        return entity

    async def updateDocument(
        self,
        session: AsyncSession,
        id: int,
        dto: DocumentUpdate,
        actor: CurrentUser,
    ) -> DocumentCatalog:
        """更新文档；不存在抛 NotFoundError。"""
        entity = await self.getDocument(session, id)
        before_state = _entity_to_dict(entity)
        updates = dto.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(entity, key, value)
        await _audit.record(
            session,
            entity_type="document_catalog",
            entity_id=entity.id,
            action="UPDATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before_state,
            after=_entity_to_dict(entity),
        )
        await session.commit()
        await session.refresh(entity)
        return entity

    async def deleteDocument(
        self, session: AsyncSession, id: int, actor: CurrentUser
    ) -> None:
        """删除文档；不存在抛 NotFoundError。级联删 document_entity_relation（ondelete CASCADE）。"""
        entity = await self.getDocument(session, id)
        before_state = _entity_to_dict(entity)
        await _audit.record(
            session,
            entity_type="document_catalog",
            entity_id=entity.id,
            action="DELETE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before_state,
        )
        await session.delete(entity)
        await session.commit()

    # -------------------------------------------------------------------------
    # DocumentEntityRelation
    # -------------------------------------------------------------------------

    async def listRelations(
        self,
        session: AsyncSession,
        *,
        document_id: str | None = None,
        entity_type: BusinessObjectCode | None = None,
        entity_key: int | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[DocumentEntityRelation]:
        """按文档或实体查询关联记录。"""
        limit = min(limit, _MAX_LIMIT)
        stmt = select(DocumentEntityRelation).order_by(DocumentEntityRelation.id)
        if document_id is not None:
            stmt = stmt.where(DocumentEntityRelation.document_id == document_id)
        if entity_type is not None:
            stmt = stmt.where(DocumentEntityRelation.entity_type == entity_type)
        if entity_key is not None:
            stmt = stmt.where(DocumentEntityRelation.entity_key == entity_key)
        stmt = stmt.limit(limit).offset(offset)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getRelation(
        self, session: AsyncSession, id: int
    ) -> DocumentEntityRelation:
        entity = await session.get(DocumentEntityRelation, id)
        if entity is None:
            raise NotFoundError(MSG_DOCUMENT_REL_NOT_FOUND.format(id=id))
        return entity

    async def createRelation(
        self,
        session: AsyncSession,
        dto: DocEntityRelationCreate,
    ) -> DocumentEntityRelation:
        """创建文档-实体关联；重复（document_id + entity + relation_type）抛 ConflictError。"""
        existing = await session.execute(
            select(DocumentEntityRelation).where(
                DocumentEntityRelation.document_id == dto.document_id,
                DocumentEntityRelation.entity_type == dto.entity_type,
                DocumentEntityRelation.entity_key == dto.entity_key,
                DocumentEntityRelation.relation_type == dto.relation_type,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(
                MSG_DOCUMENT_REL_EXISTS.format(
                    documentId=dto.document_id,
                    entityType=dto.entity_type,
                    entityKey=dto.entity_key,
                )
            )
        entity = DocumentEntityRelation(
            document_id=dto.document_id,
            entity_type=dto.entity_type,
            entity_key=dto.entity_key,
            relation_type=dto.relation_type,
        )
        session.add(entity)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_DOCUMENT_REL_EXISTS.format(
                    documentId=dto.document_id,
                    entityType=dto.entity_type,
                    entityKey=dto.entity_key,
                )
            ) from exc
        await session.refresh(entity)
        return entity

    async def deleteRelation(
        self, session: AsyncSession, id: int
    ) -> None:
        """删除关联；不存在抛 NotFoundError。"""
        entity = await self.getRelation(session, id)
        await session.delete(entity)
        await session.commit()
