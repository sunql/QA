from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.wiki_schemas import EvidenceListOut, EvidenceQuery, EvidenceRead


def test_evidence_query_accepts_minimal():
    q = EvidenceQuery()
    assert q.session_id is None
    assert q.claim_id is None
    assert q.source_type is None
    assert q.limit == 50
    assert q.offset == 0


def test_evidence_query_blank_session_id_rejected():
    with pytest.raises(ValidationError) as exc:
        EvidenceQuery(session_id="   ")
    assert "must not be blank" in str(exc.value)


def test_evidence_query_limit_bounds():
    EvidenceQuery(limit=1)
    EvidenceQuery(limit=200)
    with pytest.raises(ValidationError):
        EvidenceQuery(limit=0)
    with pytest.raises(ValidationError):
        EvidenceQuery(limit=201)


def test_evidence_query_source_type_literal():
    EvidenceQuery(source_type="SQL_QUERY")
    with pytest.raises(ValidationError):
        EvidenceQuery(source_type="BOGUS")


def test_evidence_query_clamps_whitespace_session_id():
    q = EvidenceQuery(session_id="  abc  ")
    assert q.session_id == "abc"


def test_evidence_read_carries_payload_and_session_id():
    er = EvidenceRead(
        id=1, claim_id=2, source_type="SQL_QUERY",
        payload={"sql": "SELECT 1"}, session_id="chat-123",
    )
    assert er.payload == {"sql": "SELECT 1"}
    assert er.session_id == "chat-123"


def test_evidence_read_supplements_confidence_and_content_hash():
    er = EvidenceRead(
        id=1, claim_id=2, source_type="DOCUMENT",
        content_hash="abc123", confidence=Decimal("0.95"),
    )
    assert er.content_hash == "abc123"
    assert er.confidence == Decimal("0.95")


def test_evidence_list_out_envelopes():
    out = EvidenceListOut(items=[], total=0)
    assert out.items == []
    assert out.total == 0
