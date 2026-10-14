"""Admin Wiki ↔ Ontology 链接管理 API."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getSessionDep, getAdminOnlyActor
from app.domain.models import OntologyClass, OntologyMetric, OntologyProperty
from app.domain.schemas import WikiLinkOut, WikiLinkableTargetOut
from app.services.wiki_link_service import (
    LinkNotFoundError, WikiLinkService, WikiLinkRow, LinkableTarget,
)

router = APIRouter(prefix="/api/v1/admin/wiki-links", tags=["admin-wiki-links"])


class CreateWikiLinkRequest(BaseModel):
    page_id: str = Field(..., max_length=64)
    chunk_id: str | None = Field(None, max_length=64)
    ontology_type: str = Field(..., pattern="^(class|property|metric)$")
    ontology_id: int = Field(..., gt=0)
    weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=1)
    note: str | None = Field(None, max_length=200)


class UpdateWikiLinkRequest(BaseModel):
    weight: Decimal | None = Field(None, ge=0, le=1)
    note: str | None = Field(None, max_length=200)


_ONTOLOGY_NAME_FIELDS: dict[str, tuple[Any, str, str]] = {
    "class": (OntologyClass, "class_name", "class_alias"),
    "property": (OntologyProperty, "property_name", "property_alias"),
    "metric": (OntologyMetric, "metric_name", "metric_alias"),
}


async def _resolveOntologyLabels(
    session: AsyncSession, rows: list[WikiLinkRow],
) -> dict[tuple[str, int], tuple[str | None, str | None]]:
    """批量解析 (type, id) → (name, alias)。每张本体表一次 IN 查询，避免 N+1。"""
    out: dict[tuple[str, int], tuple[str | None, str | None]] = {}
    byType: dict[str, list[int]] = {}
    for r in rows:
        byType.setdefault(r.ontology_type, []).append(r.ontology_id)

    for ontologyType, ids in byType.items():
        fields = _ONTOLOGY_NAME_FIELDS.get(ontologyType)
        if fields is None:
            continue
        model, nameAttr, aliasAttr = fields
        stmt = select(model.id, getattr(model, nameAttr), getattr(model, aliasAttr)).where(
            model.id.in_(set(ids))
        )
        for row in (await session.execute(stmt)).all():
            out[(ontologyType, row[0])] = (row[1], row[2])
    return out


async def _rowsToOut(
    session: AsyncSession, rows: list[WikiLinkRow],
) -> list[dict]:
    labels = await _resolveOntologyLabels(session, rows)
    out = []
    for r in rows:
        name, alias = labels.get((r.ontology_type, r.ontology_id), (None, None))
        out.append(WikiLinkOut(
            id=r.id, page_id=r.page_id, chunk_id=r.chunk_id,
            ontology_type=r.ontology_type, ontology_id=r.ontology_id,
            ontology_name=name, ontology_alias=alias,
            weight=float(r.weight), note=r.note, created_by=r.created_by,
            revoked_time=r.revoked_time,
        ).model_dump(mode="json"))
    return out


def _target_to_out(t: LinkableTarget) -> dict:
    return WikiLinkableTargetOut(
        id=t.id, type=t.type, name=t.name, alias=t.alias, description=t.description,
    ).model_dump(mode="json")


@router.get("")
async def list_links(
    page_id: str | None = None,
    ontology_type: str | None = None,
    ontology_id: int | None = None,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getSessionDep),
):
    svc = WikiLinkService()
    if page_id:
        rows = await svc.getLinksByPage(session, page_id)
        return await _rowsToOut(session, rows)
    rows = await svc.listAllLinks(session, ontology_type=ontology_type, ontology_id=ontology_id)
    return await _rowsToOut(session, rows)


@router.post("", status_code=201)
async def create_link(
    body: CreateWikiLinkRequest,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getSessionDep),
):
    row = await WikiLinkService().createLink(
        session,
        page_id=body.page_id,
        chunk_id=body.chunk_id,
        ontology_type=body.ontology_type,
        ontology_id=body.ontology_id,
        weight=body.weight,
        note=body.note,
        actor=_admin,
    )
    await session.commit()
    return (await _rowsToOut(session, [row]))[0]


@router.delete("/{link_id}")
async def revoke_link(
    link_id: int,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getSessionDep),
):
    try:
        row = await WikiLinkService().revokeLink(session, link_id, actor=_admin)
    except LinkNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="link not found")
    await session.commit()
    return (await _rowsToOut(session, [row]))[0]


@router.patch("/{link_id}")
async def update_link(
    link_id: int,
    body: UpdateWikiLinkRequest,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getSessionDep),
):
    try:
        row = await WikiLinkService().updateLink(
            session, link_id,
            weight=body.weight, note=body.note, actor=_admin,
        )
    except LinkNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="link not found")
    await session.commit()
    return (await _rowsToOut(session, [row]))[0]


@router.get("/linkables")
async def list_linkables(
    type: str,
    q: str | None = None,
    limit: int = 50,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getSessionDep),
):
    targets = await WikiLinkService().listLinkableTargets(session, type, query=q, limit=limit)
    return [_target_to_out(t) for t in targets]
