"""WikiVectorService 单测（feat-wiki-semantic-search）。

外部依赖（Milvus / embedding）全 mock；PG 回填用内存 Fake session 语义。
模式照 test_rag_service.py：patch 模块内符号，不连真实中间件。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.wiki_vector_service as wvs
from app.services.wiki_vector_service import (
    WikiVectorError,
    WikiVectorService,
    splitMarkdownBlocks,
)

_EMB = [0.1] * 1024


def _mockEmbSvc() -> MagicMock:
    svc = MagicMock()
    svc.generateEmbedding = AsyncMock(return_value=_EMB)
    return svc


# ---------------------------------------------------------------------------
# Markdown 切分
# ---------------------------------------------------------------------------


class TestSplitMarkdownBlocks:
    def test_splits_on_blank_lines_and_tracks_headings(self) -> None:
        content = "# 准入规则\n\n注册资本一千万以上。\n\n## 罚则\n\n违规暂停合作。"
        blocks = splitMarkdownBlocks(content)
        texts = [b.text for b in blocks]
        assert any("注册资本一千万以上" in t for t in texts)
        assert any("违规暂停合作" in t for t in texts)
        # 段落跟随最近的标题
        p1 = next(b for b in blocks if "注册资本" in b.text)
        p2 = next(b for b in blocks if "违规暂停合作" in b.text)
        assert p1.section_name == "准入规则"
        assert p2.section_name == "罚则"
        assert all(b.page_number is None for b in blocks)

    def test_empty_content_returns_empty(self) -> None:
        assert splitMarkdownBlocks("") == []
        assert splitMarkdownBlocks("   \n\n  ") == []


# ---------------------------------------------------------------------------
# syncPage / deletePageVectors 编排
# ---------------------------------------------------------------------------


def _page(**overrides) -> MagicMock:
    page = MagicMock()
    page.page_id = "PAGE-X"
    page.title = "准入规则"
    page.content = "# 规则\n\n注册资本一千万以上。"
    page.dimension = "RULE"
    page.status = "EFFECTIVE"
    for k, v in overrides.items():
        setattr(page, k, v)
    return page


class TestSyncPage:
    @pytest.mark.asyncio
    async def test_upsert_deletes_then_inserts(self) -> None:
        svc = WikiVectorService(embeddingService=_mockEmbSvc())
        with patch.object(wvs.milvus, "deleteWikiPageChunks") as mDel, \
             patch.object(wvs.milvus, "insertWikiPageChunks") as mIns:
            chunks = await svc.syncPage(_page())

        mDel.assert_called_once_with("PAGE-X")
        mIns.assert_called_once()
        records = mIns.call_args[0][0]
        assert len(records) == chunks
        assert all(r["page_id"] == "PAGE-X" for r in records)
        assert all(r["embedding"] == _EMB for r in records)
        assert records[0]["title"] == "准入规则"
        assert records[0]["status"] == "EFFECTIVE"

    @pytest.mark.asyncio
    async def test_empty_content_deletes_only(self) -> None:
        """正文清空的条目：只删旧向量，不插空 chunk。"""
        svc = WikiVectorService(embeddingService=_mockEmbSvc())
        with patch.object(wvs.milvus, "deleteWikiPageChunks") as mDel, \
             patch.object(wvs.milvus, "insertWikiPageChunks") as mIns:
            n = await svc.syncPage(_page(content=""))
        mDel.assert_called_once_with("PAGE-X")
        mIns.assert_not_called()
        assert n == 0


# ---------------------------------------------------------------------------
# searchSemantic
# ---------------------------------------------------------------------------

_HIT = {
    "page_id": "PAGE-X",
    "chunk_id": "wc-0",
    "chunk_text": "注册资本一千万以上",
    "chunk_sequence": 0,
    "title": "准入规则",
    "dimension": "RULE",
    "status": "EFFECTIVE",
    "distance": 1.0,  # score = 1/(1+1) = 0.5
}


class TestSearchSemantic:
    @pytest.mark.asyncio
    async def test_score_mapping_and_pg_hydration(self) -> None:
        svc = WikiVectorService(embeddingService=_mockEmbSvc())
        # PG 里标题已被改（SSOT 覆盖 Milvus 快照）
        pgRow = MagicMock()
        pgRow.page_id = "PAGE-X"
        pgRow.title = "准入规则 V2"
        pgRow.status = "REVIEW"
        pgRow.dimension = "RULE"

        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [pgRow]
        session.execute = AsyncMock(return_value=result)

        with patch.object(wvs.milvus, "searchWikiPageChunks", return_value=[dict(_HIT)]):
            hits = await svc.searchSemantic(session, "供应商门槛")

        assert len(hits) == 1
        assert hits[0]["score"] == pytest.approx(0.5)
        assert hits[0]["title"] == "准入规则 V2"  # PG 覆盖
        assert hits[0]["status"] == "REVIEW"
        assert hits[0]["chunkText"] == "注册资本一千万以上"

    @pytest.mark.asyncio
    async def test_pg_missing_falls_back_to_milvus_fields(self) -> None:
        svc = WikiVectorService(embeddingService=_mockEmbSvc())
        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []  # PG 查不到（已删）
        session.execute = AsyncMock(return_value=result)

        with patch.object(wvs.milvus, "searchWikiPageChunks", return_value=[dict(_HIT)]):
            hits = await svc.searchSemantic(session, "q")

        # 降级 Milvus 快照字段，不阻塞响应
        assert hits[0]["title"] == "准入规则"
        assert hits[0]["status"] == "EFFECTIVE"

    @pytest.mark.asyncio
    async def test_embedding_failure_wraps_domain_error(self) -> None:
        svc = WikiVectorService()
        svc._embSvc = None
        broken = MagicMock()
        broken.generateEmbedding = AsyncMock(side_effect=RuntimeError("provider down"))
        svc._embSvc = broken

        with pytest.raises(WikiVectorError), \
             patch.object(wvs.milvus, "searchWikiPageChunks"):
            await svc.searchSemantic(MagicMock(), "q")


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


class TestBackfill:
    @pytest.mark.asyncio
    async def test_backfill_upserts_all_pages_and_is_idempotent(self) -> None:
        svc = WikiVectorService(embeddingService=_mockEmbSvc())
        pages = [_page(page_id=f"PAGE-{i}") for i in range(3)]

        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = pages
        session.execute = AsyncMock(return_value=result)

        with patch.object(wvs.milvus, "deleteWikiPageChunks"), \
             patch.object(wvs.milvus, "insertWikiPageChunks") as mIns:
            first = await svc.backfill(session)
            second = await svc.backfill(session)

        assert first["pages"] == 3
        assert first["chunks"] > 0
        assert second == first, "重复回填幂等（upsert 语义）"
        assert mIns.call_count == 6


# ---------------------------------------------------------------------------
# embedding 计量（feat-wiki-semantic-search 后续：接入 wiki_token_usage）
# ---------------------------------------------------------------------------


class TestMetering:
    @pytest.mark.asyncio
    async def test_search_meters_prompt_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """searchSemantic 的 query embedding 必须落 wiki_token_usage（约束 #3）。"""
        metered = {}

        async def fakeMeter(*, promptTokens, modelName, purpose):
            metered.update(promptTokens=promptTokens, modelName=modelName, purpose=purpose)

        svc = WikiVectorService()
        svc._embSvc = _FakeEmbWithUsage()
        session = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        monkeypatch.setattr(wvs, "_meterEmbedding", fakeMeter)
        with patch.object(wvs.milvus, "searchWikiPageChunks", return_value=[]):
            await svc.searchSemantic(session, "供应商门槛")

        assert metered == {
            "promptTokens": 8,
            "modelName": "bge-m3-test",
            "purpose": "wiki_semantic_search",
        }

    @pytest.mark.asyncio
    async def test_sync_page_meters_aggregated_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """syncPage 逐 chunk embed 后按页聚合落一次计量。"""
        metered = {}

        async def fakeMeter(*, promptTokens, modelName, purpose):
            metered.update(promptTokens=promptTokens, purpose=purpose)

        svc = WikiVectorService()
        svc._embSvc = _FakeEmbWithUsage()

        monkeypatch.setattr(wvs, "_meterEmbedding", fakeMeter)
        with patch.object(wvs.milvus, "deleteWikiPageChunks"), \
             patch.object(wvs.milvus, "insertWikiPageChunks"):
            await svc.syncPage(_page())

        assert metered["promptTokens"] == 8  # 单段落正文 → 1 chunk × 8
        assert metered["purpose"] == "wiki_vector_sync"

    @pytest.mark.asyncio
    async def test_zero_tokens_skips_metering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """provider 未报 usage（tokens=0）→ 不落行，绝不编造估算值。

        guard 在 _meterEmbedding 内部（<=0 短路），故不 patch 它本身，
        而是盯住更深的 DB 入口：getSessionFactory 不该被触碰。
        """
        factoryCalls = []

        def fakeFactory():
            factoryCalls.append(1)
            raise AssertionError("tokens=0 时不得开计量 session")

        monkeypatch.setattr("app.infrastructure.database.getSessionFactory", fakeFactory)

        svc = WikiVectorService()
        svc._embSvc = _mockEmbSvc()  # 只有 generateEmbedding → 回退路径 tokens=0

        with patch.object(wvs.milvus, "deleteWikiPageChunks"), \
             patch.object(wvs.milvus, "insertWikiPageChunks"):
            await svc.syncPage(_page())  # 不抛即通过

        assert factoryCalls == []


class _FakeEmbWithUsage:
    """带 embedWithUsage 的测试替身（模拟真实 EmbeddingService 门面契约）。"""

    async def embedWithUsage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int, str | None]:
        return [[0.1] * 1024 for _ in texts], 8 * len(texts), "bge-m3-test"

    async def generateEmbedding(self, text: str) -> list[float]:
        return [0.1] * 1024
