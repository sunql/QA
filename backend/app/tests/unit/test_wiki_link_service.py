"""WikiLinkService 单测（wiki-ontology-link Task 2，real PG per Harness 测试规范）。

覆盖：CRUD 5 个 + recall 4 个 + listLinkableTargets 3 个。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, ValidationError
from app.domain.models import WikiOntologyLink
from app.services.wiki_link_service import (
    LinkNotFoundError, WikiLinkService,
)

_svc = WikiLinkService()


async def _seed_page(dbSession: AsyncSession, page_id: str = "p001") -> None:
    """插入 wiki_page 行（FK 目标）。"""
    from app.domain.models import WikiPage
    dbSession.add(WikiPage(
        page_id=page_id, title="测试页", dimension="test",
        content="content", status="PUBLISHED",
        created_time=datetime.now(timezone.utc), updated_time=datetime.now(timezone.utc),
    ))
    await dbSession.flush()


async def _actor(userId: int = 42):
    return type("Actor", (), {"userId": userId})()


async def test_create_link_persists_row(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    assert row.page_id == "p001"
    assert row.ontology_type == "class"
    assert row.ontology_id == 12
    assert row.weight == Decimal("1.00")


async def test_create_link_conflict_returns_409(dbSession):
    await _seed_page(dbSession)
    await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    with pytest.raises(ConflictError):
        await _svc.createLink(
            dbSession,
            page_id="p001", chunk_id=None, ontology_type="class",
            ontology_id=12, weight=Decimal("1.00"), note=None,
            actor=await _actor(42),
        )


async def test_revoke_link_sets_revoked_time(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    revoked = await _svc.revokeLink(dbSession, row.id, actor=await _actor(42))
    assert revoked.revoked_time is not None


async def test_revoke_link_not_found(dbSession):
    with pytest.raises(LinkNotFoundError):
        await _svc.revokeLink(dbSession, 99999, actor=await _actor(42))


async def test_update_link_changes_weight_and_note(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    updated = await _svc.updateLink(
        dbSession, row.id, weight=Decimal("0.5"), note="updated",
        actor=await _actor(42),
    )
    assert updated.weight == Decimal("0.5")
    assert updated.note == "updated"


async def test_get_links_by_ontology_filters_unrevoked_pairs(dbSession):
    await _seed_page(dbSession)
    await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    pairs = [("class", 12), ("property", 99)]
    rows = await _svc.getLinksByOntology(dbSession, pairs)
    assert len(rows) == 1
    assert rows[0].ontology_id == 12


async def test_get_links_by_ontology_skips_revoked(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    await _svc.revokeLink(dbSession, row.id, actor=await _actor(42))
    rows = await _svc.getLinksByOntology(dbSession, [("class", 12)])
    assert rows == []


async def test_get_links_by_page_returns_all(dbSession):
    await _seed_page(dbSession, page_id="p001")
    await _seed_page(dbSession, page_id="p002")
    await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    await _svc.createLink(
        dbSession,
        page_id="p002", chunk_id="c001", ontology_type="property",
        ontology_id=99, weight=Decimal("0.5"), note=None,
        actor=await _actor(42),
    )
    rows = await _svc.getLinksByPage(dbSession, "p001")
    assert len(rows) == 1
    assert rows[0].page_id == "p001"


async def test_get_links_by_ontology_empty_pairs_returns_empty(dbSession):
    rows = await _svc.getLinksByOntology(dbSession, [])
    assert rows == []


async def test_list_linkable_targets_filters_by_type_class(dbSession):
    targets = await _svc.listLinkableTargets(dbSession, "class", query=None, limit=10)
    assert all(t.type == "class" for t in targets)


async def test_list_linkable_targets_query_filters_by_name(dbSession):
    # Seed a known class so we can verify query filtering
    from app.domain.models import OntologyClass
    dbSession.add(OntologyClass(class_name="DIM_SUPPLIER", class_alias="供应商", description="供应商维度"))
    await dbSession.flush()
    targets = await _svc.listLinkableTargets(dbSession, "class", query="SUPPLIER", limit=10)
    assert len(targets) >= 1
    assert any("SUPPLIER" in t.name.upper() for t in targets)


async def test_list_linkable_targets_invalid_type_returns_empty(dbSession):
    targets = await _svc.listLinkableTargets(dbSession, "metric", query=None, limit=10)
    assert targets == []


async def test_create_link_invalid_type_returns_422(dbSession):
    await _seed_page(dbSession)
    with pytest.raises(ValidationError):
        await _svc.createLink(
            dbSession,
            page_id="p001", chunk_id=None, ontology_type="metric",
            ontology_id=12, weight=Decimal("1.00"), note=None,
            actor=await _actor(42),
        )
