"""evidence_query_service unit tests with mocked AsyncSession."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.wiki_schemas import EvidenceQuery
from app.services.evidence_query_service import (
    getEvidenceById,
    listEvidences,
    listEvidencesBySession,
)


def _make_session_with_scalars(items, total):
    """Build a mock session whose .scalars().all() returns items and .scalar() returns total."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = items
    session.scalars.return_value = scalars_mock
    session.scalar.return_value = total
    return session


@pytest.mark.asyncio
async def test_listEvidences_returns_items_and_total():
    items = [MagicMock(), MagicMock()]
    session = _make_session_with_scalars(items, total=2)
    query = EvidenceQuery(session_id="chat-1", source_type="SQL_QUERY")

    result_items, total = await listEvidences(session, query)

    assert list(result_items) == list(items)
    assert total == 2


@pytest.mark.asyncio
async def test_listEvidencesBySession_short_circuits_other_filters():
    items = [MagicMock()]
    session = _make_session_with_scalars(items, total=1)

    result_items, total = await listEvidencesBySession(
        session, session_id="chat-1", limit=10, offset=0
    )

    assert list(result_items) == list(items)
    assert total == 1


@pytest.mark.asyncio
async def test_getEvidenceById_returns_none_when_missing():
    session = AsyncMock()
    session.scalar.return_value = None
    result = await getEvidenceById(session, 9999)
    assert result is None
