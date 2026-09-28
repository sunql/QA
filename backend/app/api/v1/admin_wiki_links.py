"""Admin Wiki ↔ Ontology 链接管理 API."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getSessionDep, getAdminOnlyActor
from app.domain.schemas import WikiLinkOut, WikiLinkableTargetOut
from app.services.wiki_link_service import (
    LinkNotFoundError, WikiLinkService, WikiLinkRow, LinkableTarget,
)

router = APIRouter(prefix="/api/v1/admin/wiki-links", tags=["admin-wiki-links"])


class CreateWikiLinkRequest(BaseModel):
    page_id: str = Field(..., max_length=64)
    chunk_id: str | None = Field(None, max_length=64)
    ontology_type: str = Field(..., pattern="^(class|property)$")
    ontology_id: int = Field(..., gt=0)
    weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=1)
    note: str | None = Field(None, max_length=200)


class UpdateWikiLinkRequest(BaseModel):
    weight: Decimal | None = Field(None, ge=0, le=1)
    note: str | None = Field(None, max_length=200)


def _row_to_out(r: WikiLinkRow) -> dict:
    return WikiLinkOut(
        id=r.id, page_id=r.page_id, chunk_id=r.chunk_id,
        ontology_type=r.ontology_type, ontology_id=r.ontology_id,
        weight=r.weight, note=r.note, created_by=r.created_by,
        revoked_time=r.revoked_time,
    ).model_dump(mode="json")


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
    else:
        rows = await svc.listAllLinks(session, ontology_type=ontology_type, ontology_id=ontology_id)
    return [_row_to_out(r) for r in rows]


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
    return _row_to_out(row)


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
    return _row_to_out(row)


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
    return _row_to_out(row)


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
