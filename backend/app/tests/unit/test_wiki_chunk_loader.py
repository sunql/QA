import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.wiki_chunk_loader import WikiChunkLoader


async def test_load_chunk_level_chunks_from_milvus(dbSession, monkeypatch):
    # Mock Milvus 查询：返回 (page_id, chunk_id, chunk_text) 三元组

    class FakeMilvusHits(list):
        def __init__(self):
            super().__init__([
                {"page_id": "p001", "chunk_id": "c005", "chunk_text": "rule from chunk"},
            ])

    async def fake_search(page_id, chunk_id):
        return [("p001", "c005", "rule from chunk")]

    monkeypatch.setattr(
        "app.services.wiki_chunk_loader._searchWikiChunks",
        fake_search,
    )

    out = await WikiChunkLoader().loadChunks(
        dbSession, page_ids=["p001"], chunk_ids=["c005"]
    )
    assert out == {("p001", "c005"): "rule from chunk"}


async def test_load_page_level_chunks_from_pg(dbSession):
    # PG wiki_page.content
    from app.domain.wiki_models import WikiPage
    from datetime import datetime, UTC
    dbSession.add(WikiPage(
        page_id="p002", title="page", dimension="t",
        content="page markdown content",
        status="PUBLISHED",
        created_time=datetime.now(UTC), updated_time=datetime.now(UTC),
    ))
    await dbSession.flush()

    out = await WikiChunkLoader().loadChunks(
        dbSession, page_ids=["p002"], chunk_ids=[None]
    )
    assert out[("p002", "")] == "page markdown content"


async def test_load_missing_chunk_returns_no_key(dbSession):
    out = await WikiChunkLoader().loadChunks(
        dbSession, page_ids=["pXXX"], chunk_ids=["cYYY"]
    )
    assert out == {}


async def test_load_page_content_truncated_to_max_chars(dbSession):
    from app.domain.wiki_models import WikiPage
    from datetime import datetime, UTC
    long_content = "x" * 8000
    dbSession.add(WikiPage(
        page_id="p003", title="p", dimension="t",
        content=long_content, status="PUBLISHED",
        created_time=datetime.now(UTC), updated_time=datetime.now(UTC),
    ))
    await dbSession.flush()

    out = await WikiChunkLoader().loadChunks(
        dbSession, page_ids=["p003"], chunk_ids=[None]
    )
    # _PAGE_CONTENT_MAX_CHARS = 4000
    assert len(out[("p003", "")]) == 4000
