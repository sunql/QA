"""Wiki ↔ Ontology 链接管理服务。

提供 admin CRUD 与 NL2SQL recall 查询。所有方法只读写 PG，
不调 LLM、不调 Milvus（chunk 加载由 WikiChunkLoader 负责）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import (
    OntologyClass, OntologyProperty, WikiOntologyLink,
)


class LinkNotFoundError(NotFoundError):
    pass


_VALID_ONTOLOGY_TYPES = frozenset({"class", "property"})


@dataclass(frozen=True)
class WikiLinkRow:
    id: int
    page_id: str
    chunk_id: str | None
    ontology_type: str
    ontology_id: int
    weight: Decimal
    note: str | None
    created_by: int
    revoked_time: datetime | None


@dataclass(frozen=True)
class LinkableTarget:
    id: int
    type: str  # 'class' | 'property'
    name: str
    alias: str | None
    description: str | None


class WikiLinkService:
    """Wiki ↔ Ontology 链接的 CRUD + 查询。"""

    async def createLink(
        self,
        session: AsyncSession,
        *,
        page_id: str,
        chunk_id: str | None,
        ontology_type: str,
        ontology_id: int,
        weight: Decimal,
        note: str | None,
        actor: Any,
    ) -> WikiLinkRow:
        if ontology_type not in _VALID_ONTOLOGY_TYPES:
            raise ValidationError(f"ontology_type must be one of {_VALID_ONTOLOGY_TYPES}")
        if not (Decimal("0") <= weight <= Decimal("1")):
            raise ValidationError("weight must be between 0 and 1")
        row = WikiOntologyLink(
            page_id=page_id,
            chunk_id=chunk_id,
            ontology_type=ontology_type,
            ontology_id=ontology_id,
            weight=weight,
            note=note,
            created_by=actor.userId,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            raise ConflictError(
                "该 page+chunk+type+ontology 链接已存在或 page 不存在"
            ) from e
        return _to_row(row)

    async def revokeLink(
        self,
        session: AsyncSession,
        link_id: int,
        *,
        actor: Any,
    ) -> WikiLinkRow:
        row = await session.get(WikiOntologyLink, link_id)
        if row is None:
            raise LinkNotFoundError(f"link {link_id} not found")
        row.revoked_time = datetime.now(UTC)
        await session.flush()
        return _to_row(row)

    async def updateLink(
        self,
        session: AsyncSession,
        link_id: int,
        *,
        weight: Decimal | None,
        note: str | None,
        actor: Any,
    ) -> WikiLinkRow:
        row = await session.get(WikiOntologyLink, link_id)
        if row is None:
            raise LinkNotFoundError(f"link {link_id} not found")
        if weight is not None:
            if not (Decimal("0") <= weight <= Decimal("1")):
                raise ValidationError("weight must be between 0 and 1")
            row.weight = weight
        if note is not None:
            row.note = note
        await session.flush()
        return _to_row(row)

    async def getLinksByOntology(
        self,
        session: AsyncSession,
        pairs: list[tuple[str, int]],
    ) -> list[WikiLinkRow]:
        """按 (ontology_type, ontology_id) 元组列表查所有未撤销链接。

        元组 IN 走 ix_wol_ontology 索引；空列表直接返 []。
        """
        if not pairs:
            return []
        conditions = [
            and_(
                WikiOntologyLink.ontology_type == t,
                WikiOntologyLink.ontology_id == i,
            )
            for (t, i) in pairs
            if t in _VALID_ONTOLOGY_TYPES
        ]
        if not conditions:
            return []
        stmt = (
            select(WikiOntologyLink)
            .where(
                or_(*conditions),
                WikiOntologyLink.revoked_time.is_(None),
            )
        )
        result = await session.execute(stmt)
        return [_to_row(r) for r in result.scalars().all()]

    async def getLinksByPage(
        self,
        session: AsyncSession,
        page_id: str,
    ) -> list[WikiLinkRow]:
        stmt = (
            select(WikiOntologyLink)
            .where(
                WikiOntologyLink.page_id == page_id,
                WikiOntologyLink.revoked_time.is_(None),
            )
        )
        result = await session.execute(stmt)
        return [_to_row(r) for r in result.scalars().all()]

    async def listLinkableTargets(
        self,
        session: AsyncSession,
        type: str,
        *,
        query: str | None = None,
        limit: int = 50,
    ) -> list[LinkableTarget]:
        """给 admin 弹窗选择器用：列 ontology_class 或 ontology_property。"""
        if type == "class":
            stmt = select(OntologyClass)
            if query:
                pattern = f"%{query.upper()}%"
                stmt = stmt.where(
                    or_(
                        OntologyClass.class_name.ilike(pattern),
                        OntologyClass.class_alias.ilike(pattern),
                    )
                )
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                LinkableTarget(
                    id=r.id,
                    type="class",
                    name=r.class_name,
                    alias=r.class_alias,
                    description=r.description,
                )
                for r in rows
            ]
        if type == "property":
            stmt = select(OntologyProperty)
            if query:
                pattern = f"%{query.upper()}%"
                stmt = stmt.where(
                    or_(
                        OntologyProperty.property_name.ilike(pattern),
                        OntologyProperty.property_alias.ilike(pattern),
                    )
                )
            stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                LinkableTarget(
                    id=r.id,
                    type="property",
                    name=r.property_name,
                    alias=r.property_alias,
                    description=r.description,
                )
                for r in rows
            ]
        return []


def _to_row(r: WikiOntologyLink) -> WikiLinkRow:
    return WikiLinkRow(
        id=r.id,
        page_id=r.page_id,
        chunk_id=r.chunk_id,
        ontology_type=r.ontology_type,
        ontology_id=r.ontology_id,
        weight=r.weight,
        note=r.note,
        created_by=r.created_by,
        revoked_time=r.revoked_time,
    )
