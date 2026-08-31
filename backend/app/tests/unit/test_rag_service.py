"""Phase 5.2 rag_service 单测（TDD 顺序）。

外部依赖：parse_document / split_by_paragraphs / embedding_service / Milvus → 全 mock。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rag_service import RagError, RagService


class TestRagServiceSearch:
    """searchDocuments: embedding → Milvus 查询。"""

    @pytest.mark.asyncio
    async def test_searchDocuments_returns_hits(self) -> None:
        mock_emb_svc = AsyncMock()
        mock_emb_svc.embed_texts = AsyncMock(return_value=[[0.1] * 1024])

        mock_hit = {
            "document_id": "DOC-001",
            "chunk_id": "chunk-0",
            "chunk_text": "测试文本内容",
            "chunk_sequence": 0,
            "distance": 0.42,
        }

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mock_emb_svc,
        ):
            with patch(
                "app.services.rag_service.searchDocumentChunks",
                return_value=[mock_hit],
            ) as mock_search:
                svc = RagService()
                hits = await svc.searchDocuments(
                    "测试查询",
                    security_level="L1",
                    top_k=5,
                )

        assert len(hits) == 1
        assert hits[0]["document_id"] == "DOC-001"
        assert hits[0]["chunk_text"] == "测试文本内容"
        mock_emb_svc.embed_texts.assert_called_once_with(["测试查询"])
        mock_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_searchDocuments_empty_result(self) -> None:
        mock_emb_svc = AsyncMock()
        mock_emb_svc.embed_texts = AsyncMock(return_value=[[0.1] * 1024])

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mock_emb_svc,
        ):
            with patch(
                "app.services.rag_service.searchDocumentChunks",
                return_value=[],
            ) as mock_search:
                svc = RagService()
                hits = await svc.searchDocuments("无结果查询")

        assert hits == []
        mock_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_searchDocuments_embedding_failure_raises(self) -> None:
        mock_emb_svc = AsyncMock()
        mock_emb_svc.embed_texts = AsyncMock(
            side_effect=RuntimeError("模型不可用")
        )

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mock_emb_svc,
        ):
            svc = RagService()
            with pytest.raises(RagError, match="Query embedding 失败"):
                await svc.searchDocuments("测试查询")


class TestRagServiceIngest:
    """ingestDocument: 解析 → 分块 → embedding → Milvus → catalog。"""

    def _mock_doc_svc_with_create(self):
        """返回一个正确配置 mock doc_svc（createDocument 幂等）。"""
        svc = MagicMock()
        svc.listDocuments = AsyncMock(return_value=[])
        svc.createDocument = AsyncMock()
        svc.getDocument = AsyncMock(side_effect=Exception("not found"))
        svc.updateDocument = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_ingestDocument_full_pipeline(self) -> None:
        mock_session = MagicMock()
        mock_chunks = [
            MagicMock(chunk_id="chunk-0", text="这是测试文档内容。", sequence=0),
        ]
        mock_emb = AsyncMock()
        mock_emb.embed_texts = AsyncMock(return_value=[[0.1] * 1024])

        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value="这是测试文档内容。",
        ):
            with patch(
                "app.services.rag_service.split_by_paragraphs",
                return_value=mock_chunks,
            ):
                with patch(
                    "app.services.rag_service._getEmbeddingService",
                    return_value=mock_emb,
                ):
                    with patch(
                        "app.services.rag_service.insertDocumentChunks",
                    ) as mock_insert:
                        svc = RagService()
                        svc._doc_svc = self._mock_doc_svc_with_create()

                        result = await svc.ingestDocument(
                            mock_session,
                            content=b"dummy",
                            filename="test.txt",
                            mime_type="text/plain",
                            document_id="DOC-TEST-001",
                            document_name="测试文档",
                            document_type="CONTRACT",
                        )

        assert result["document_id"] == "DOC-TEST-001"
        assert result["status"] == "ingested"
        assert result["chunks"] == 1
        mock_insert.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingestDocument_parser_failure_raises(self) -> None:
        from app.services.document_parser import DocumentParserError

        mock_session = MagicMock()
        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            side_effect=DocumentParserError("unsupported"),
        ):
            svc = RagService()
            with pytest.raises(RagError, match="文档解析失败"):
                await svc.ingestDocument(
                    mock_session,
                    content=b"dummy",
                    filename="file.bin",
                    mime_type="application/octet-stream",
                )

    @pytest.mark.asyncio
    async def test_ingestDocument_empty_text_raises(self) -> None:
        mock_session = MagicMock()
        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value="   \n\t  ",
        ):
            svc = RagService()
            with pytest.raises(RagError, match="文档内容为空"):
                await svc.ingestDocument(
                    mock_session,
                    content=b"",
                    filename="empty.txt",
                    mime_type="text/plain",
                )

    @pytest.mark.asyncio
    async def test_ingestDocument_milvus_failure_raises(self) -> None:
        mock_session = MagicMock()
        mock_emb = AsyncMock()
        mock_emb.embed_texts = AsyncMock(return_value=[[0.1] * 1024])

        with patch(
            "app.services.rag_service.parse_document",
            new_callable=AsyncMock,
            return_value="测试内容",
        ):
            with patch(
                "app.services.rag_service.split_by_paragraphs",
                return_value=[
                    MagicMock(chunk_id="c0", text="测试", sequence=0),
                ],
            ):
                with patch(
                    "app.services.rag_service._getEmbeddingService",
                    return_value=mock_emb,
                ):
                    with patch(
                        "app.services.rag_service.insertDocumentChunks",
                        side_effect=RuntimeError("Milvus unavailable"),
                    ):
                        svc = RagService()
                        svc._doc_svc = self._mock_doc_svc_with_create()

                        with pytest.raises(RagError, match="Milvus 写入失败"):
                            await svc.ingestDocument(
                                mock_session,
                                content=b"test",
                                filename="test.txt",
                                mime_type="text/plain",
                            )
