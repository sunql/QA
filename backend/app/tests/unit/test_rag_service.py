"""Phase 5.2 rag_service 单测（TDD 顺序）。

外部依赖：parse_document / split_by_paragraphs / embedding_service / Milvus → 全 mock。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rag_service import RagError, RagService
from app.dependencies import CurrentUser


class TestRagServiceSearch:
    """searchDocuments: embedding → Milvus 查询。"""

    @pytest.mark.asyncio
    async def test_searchDocuments_returns_hits(self) -> None:
        mock_emb_svc = AsyncMock()
        mock_emb_svc.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)

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
        mock_emb_svc.generateEmbedding.assert_called_once_with("测试查询")
        mock_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_searchDocuments_exposes_score_from_distance(self) -> None:
        """searchDocuments 必须把 Milvus L2 distance 转成 [0, 1] 的相似度 score。

        公式：score = 1 / (1 + distance)，把 [0, ∞) 映射到 (0, 1]。
        distance=0 → 1.0；distance=1 → 0.5；distance→∞ → 0。

        契约 — DocumentsPage.tsx 直接读 item.score 并渲染成百分比，
        缺失 score 时显示 NaN%（item.score * 100 = NaN）。
        """
        mock_emb_svc = AsyncMock()
        mock_emb_svc.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)

        mock_hits = [
            {
                "document_id": "DOC-A",
                "chunk_id": "c0",
                "chunk_text": "完全相同",
                "chunk_sequence": 0,
                "distance": 0.0,
            },
            {
                "document_id": "DOC-B",
                "chunk_id": "c1",
                "chunk_text": "中等相似",
                "chunk_sequence": 1,
                "distance": 1.0,
            },
            {
                "document_id": "DOC-C",
                "chunk_id": "c2",
                "chunk_text": "很不相似",
                "chunk_sequence": 2,
                "distance": 9.0,
            },
        ]

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mock_emb_svc,
        ):
            with patch(
                "app.services.rag_service.searchDocumentChunks",
                return_value=mock_hits,
            ):
                svc = RagService()
                hits = await svc.searchDocuments("查询")

        assert len(hits) == 3
        # distance=0 → score=1.0（最高相似度）
        assert hits[0]["score"] == pytest.approx(1.0)
        # distance=1 → score=0.5
        assert hits[1]["score"] == pytest.approx(0.5)
        # distance=9 → score=0.1
        assert hits[2]["score"] == pytest.approx(0.1)
        # score 字段必须保留原 distance 给调试用
        assert hits[0]["distance"] == 0.0
        assert hits[1]["distance"] == 1.0

    @pytest.mark.asyncio
    async def test_searchDocuments_hydrates_document_name_from_catalog(self) -> None:
        """searchDocuments 必须用 document_id JOIN document_catalog，拿到 document_name。

        契约 — DocumentsPage.tsx 渲染 Card 标题用 item.document_name；
        没有这字段时显示空白（用户看到的 DOC-xxxx 内部编码变成 — 无意义）。
        """
        mock_emb_svc = AsyncMock()
        mock_emb_svc.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)

        mock_hits = [
            {
                "document_id": "DOC-A",
                "chunk_id": "c0",
                "chunk_text": "合同条款",
                "chunk_sequence": 0,
                "distance": 0.5,
            },
            {
                "document_id": "DOC-B",
                "chunk_id": "c1",
                "chunk_text": "另一份",
                "chunk_sequence": 0,
                "distance": 1.0,
            },
        ]

        # 模拟 document_catalog：DOC-A 有名字；DOC-B 没有（catalog 里被删/导入失败）
        class _Doc:
            def __init__(self, document_id: str, document_name: str) -> None:
                self.document_id = document_id
                self.document_name = document_name

        catalog_docs = [_Doc("DOC-A", "供应商合同 V2.0")]

        mock_doc_svc = MagicMock()
        mock_doc_svc.listDocuments = AsyncMock(return_value=catalog_docs)

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mock_emb_svc,
        ):
            with patch(
                "app.services.rag_service.searchDocumentChunks",
                return_value=mock_hits,
            ):
                svc = RagService()
                svc._doc_svc = mock_doc_svc

                hits = await svc.searchDocuments("查询", session=MagicMock())

        # DOC-A 必须查到名字
        assert hits[0]["document_name"] == "供应商合同 V2.0"
        # DOC-B 查不到时降级用 document_id 本身（不返回 undefined）
        assert hits[1]["document_name"] == "DOC-B"
        # 调用 listDocuments 时必须带上 document_ids 过滤（避免全表扫）
        mock_doc_svc.listDocuments.assert_called_once()
        kwargs = mock_doc_svc.listDocuments.call_args.kwargs
        assert "document_ids" in kwargs
        assert set(kwargs["document_ids"]) == {"DOC-A", "DOC-B"}

    @pytest.mark.asyncio
    async def test_searchDocuments_empty_result(self) -> None:
        mock_emb_svc = AsyncMock()
        mock_emb_svc.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)

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
        mock_emb_svc.generateEmbedding = AsyncMock(
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
        mock_emb.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)

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
                            actor=CurrentUser(userId="test-user"),
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
                    actor=CurrentUser(userId="test-user"),
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
                    actor=CurrentUser(userId="test-user"),
                )

    @pytest.mark.asyncio
    async def test_ingestDocument_milvus_failure_raises(self) -> None:
        mock_session = MagicMock()
        mock_emb = AsyncMock()
        mock_emb.generateEmbedding = AsyncMock(return_value=[0.1] * 1024)

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
                                actor=CurrentUser(userId="test-user"),
                            )
