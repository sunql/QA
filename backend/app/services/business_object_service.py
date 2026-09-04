"""业务对象注册表 CRUD service（Phase 4.4）.

SSOT = business_object 表；Neo4j label 与 ontology_class.class_name 对齐。
守卫：
  - create / update: graph_label 必须 = header_class.class_name（或两者皆 NULL）
  - delete: 三表引用检查 (entity_mapping / feature_definition / document_entity_relation)
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import (
    BusinessObjectGraphLabelMismatchError,
    ConflictError,
    NotFoundError,
)
from app.domain.models import (
    BusinessObject,
    DocumentEntityRelation,
    EntityMapping,
    FeatureDefinition,
    OntologyClass,
)
from app.domain.schemas import (
    BusinessObjectCreate,
    BusinessObjectUpdate,
)
from app.services.messages_zh import (
    MSG_BUSINESS_OBJECT_CODE_EXISTS,
    MSG_BUSINESS_OBJECT_IN_USE,
    MSG_BUSINESS_OBJECT_NOT_FOUND,
)

_REFERENCING_TABLES = (
    ("entity_mapping", EntityMapping),
    ("feature_definition", FeatureDefinition),
    ("document_entity_relation", DocumentEntityRelation),
)


class BusinessObjectService:
    """业务对象 CRUD + 守卫."""

    async def listObjects(self, session: AsyncSession) -> list[BusinessObject]:
        rows = (
            await session.execute(
                select(BusinessObject).order_by(BusinessObject.code)
            )
        ).scalars().all()
        return list(rows)

    async def getObject(
        self, session: AsyncSession, code: str
    ) -> BusinessObject:
        row = await session.get(BusinessObject, code)
        if row is None:
            raise NotFoundError(MSG_BUSINESS_OBJECT_NOT_FOUND.format(code=code))
        return row

    async def _assertGraphLabelMatches(
        self,
        session: AsyncSession,
        code: str,
        header_class_id: int | None,
        graph_label: str | None,
    ) -> None:
        """graph_label 必须 = header_class.class_name; 二者皆 NULL 也合法."""
        if header_class_id is None and graph_label is None:
            return
        if header_class_id is None or graph_label is None:
            raise BusinessObjectGraphLabelMismatchError(code, graph_label or "", "")
        row = (
            await session.execute(
                select(OntologyClass.class_name).where(
                    OntologyClass.id == header_class_id
                )
            )
        ).first()
        if row is None:
            raise ConflictError(
                f"本体类 id={header_class_id} 不存在"
            )
        class_name = str(row[0])
        if class_name != graph_label:
            raise BusinessObjectGraphLabelMismatchError(code, graph_label, class_name)

    async def createObject(
        self,
        session: AsyncSession,
        dto: BusinessObjectCreate,
        actor: str,
    ) -> BusinessObject:
        # 1) 重复检查
        existing = await session.get(BusinessObject, dto.code)
        if existing is not None:
            raise ConflictError(
                MSG_BUSINESS_OBJECT_CODE_EXISTS.format(code=dto.code)
            )
        # 2) graph_label 一致性
        await self._assertGraphLabelMatches(
            session, dto.code, dto.header_class_id, dto.graph_label
        )
        # 3) 构造新 ORM 对象（不可变）
        obj = BusinessObject(
            code=dto.code,
            name=dto.name,
            header_class_id=dto.header_class_id,
            graph_label=dto.graph_label,
            description=dto.description,
            created_by=actor,
        )
        session.add(obj)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ConflictError(
                MSG_BUSINESS_OBJECT_CODE_EXISTS.format(code=dto.code)
            ) from exc
        await session.refresh(obj)
        return obj

    async def updateObject(
        self,
        session: AsyncSession,
        code: str,
        dto: BusinessObjectUpdate,
    ) -> BusinessObject:
        row = await self.getObject(session, code)
        updates = dto.model_dump(exclude_unset=True)
        if "graph_label" in updates or "header_class_id" in updates:
            new_label = updates.get("graph_label", row.graph_label)
            new_class_id = updates.get("header_class_id", row.header_class_id)
            await self._assertGraphLabelMatches(
                session, code, new_class_id, new_label
            )
        for field, value in updates.items():
            setattr(row, field, value)
        await session.commit()
        await session.refresh(row)
        return row

    async def deleteObject(
        self, session: AsyncSession, code: str
    ) -> None:
        row = await self.getObject(session, code)
        # 引用检查：三个表任一有引用 → 409
        referenced: list[str] = []
        for table_name, model in _REFERENCING_TABLES:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(getattr(model, "entity_type") == code)
                )
            ).scalar()
            if count:
                referenced.append(table_name)
        if referenced:
            raise ConflictError(
                MSG_BUSINESS_OBJECT_IN_USE.format(
                    code=code, tables=", ".join(referenced)
                )
            )
        await session.delete(row)
        await session.commit()
