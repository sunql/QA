"""Integration tests for /evidences API.

Uses real PG (qa_metadata_test) + full FastAPI app. Each test:
1. truncate evidence + knowledge_claim (via client fixture)
2. arrange: create WikiPage + claim + evidence rows via dbSession fixture
3. act: hit endpoint with `client`
4. assert: response shape + DB state

Routes-by-session MUST register before /{evidence_id} (wiki search endpoint lesson).
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence, KnowledgeClaim


pytestmark = pytest.mark.integration


async def _seedClaimAndEvidences(
    session: AsyncSession, client: AsyncClient
) -> tuple[int, list[int]]:
    """返回 (claim_id, [evidence_ids])，seed 三种类型 evidence。

    WikiPage 通过 public API 创建（满足 KnowledgeClaim FK constraint）。
    """
    # Create WikiPage via public API so FK constraint is satisfied
    created = await client.post(
        "/api/v1/wiki/pages", json={"title": "test", "content": "x"}
    )
    assert created.status_code == 201
    pageId = created.json()["pageId"]

    claim = KnowledgeClaim(page_id=pageId, claim_text="test claim")
    session.add(claim)
    await session.flush()
    e1 = Evidence(
        claim_id=claim.id,
        source_type="DOCUMENT",
        content_hash="h-doc",
        content="doc body",
    )
    e2 = Evidence(
        claim_id=claim.id,
        source_type="SQL_QUERY",
        session_id="chat-abc",
        payload={"sql": "SELECT 1", "result_hash": "abc123"},
    )
    e3 = Evidence(
        claim_id=claim.id,
        source_type="METRIC_RESULT",
        session_id="chat-xyz",
        payload={"metric_code": "SA", "value": 100},
    )
    session.add_all([e1, e2, e3])
    await session.commit()
    return claim.id, [e1.id, e2.id, e3.id]


async def test_list_filters_by_session_id(
    client: AsyncClient, dbSession: AsyncSession
):
    _, [_, e2, _] = await _seedClaimAndEvidences(dbSession, client)
    resp = await client.get("/api/v1/evidences", params={"session_id": "chat-abc"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == e2
    assert body["items"][0]["sourceType"] == "SQL_QUERY"
    assert body["items"][0]["payload"]["sql"] == "SELECT 1"


async def test_list_filters_by_source_type_and_claim_id(
    client: AsyncClient, dbSession: AsyncSession
):
    claim_id, [_, _, _] = await _seedClaimAndEvidences(dbSession, client)
    resp = await client.get(
        "/api/v1/evidences",
        params={"claim_id": claim_id, "source_type": "METRIC_RESULT"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["sourceType"] == "METRIC_RESULT"


async def test_list_blank_session_id_422(client: AsyncClient):
    resp = await client.get("/api/v1/evidences", params={"session_id": "   "})
    assert resp.status_code == 422


async def test_list_invalid_source_type_422(client: AsyncClient):
    resp = await client.get("/api/v1/evidences", params={"source_type": "BOGUS"})
    assert resp.status_code == 422


async def test_get_by_id_returns_evidence(
    client: AsyncClient, dbSession: AsyncSession
):
    _, [_, e2, _] = await _seedClaimAndEvidences(dbSession, client)
    resp = await client.get(f"/api/v1/evidences/{e2}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == e2
    assert body["payload"]["sql"] == "SELECT 1"


async def test_get_by_id_404_when_missing(client: AsyncClient):
    resp = await client.get("/api/v1/evidences/999999")
    assert resp.status_code == 404


async def test_by_session_route_not_shadowed_by_id_route(
    client: AsyncClient, dbSession: AsyncSession
):
    """Routes must register by-session BEFORE /{evidence_id}.

    If /{evidence_id} shadows, GET /evidences/by-session/chat-abc would
    try to parse 'by-session' as int and return 422 (not 200).
    """
    _, [_, _, _] = await _seedClaimAndEvidences(dbSession, client)
    resp = await client.get("/api/v1/evidences/by-session/chat-abc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["sessionId"] == "chat-abc"


async def test_list_does_not_break_when_no_evidences(client: AsyncClient):
    resp = await client.get("/api/v1/evidences")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}
