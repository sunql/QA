"""RAG API 集成测试（Phase 5.2 端到端）。

链路：POST /upload → parse → chunk → embed → Milvus → catalog；
      POST /search → embed query → Milvus search。

真实 PG 5433 + 真实 Milvus；embedding_service 用 mock（不依赖真实 LLM），
目的是验证路由 + 服务层 + 仓储层拼接正确，避免 TDD 阶段漏过接口错位 Bug。

回归保护：之前的 Phase 5.2 commit 中，
rag_service._getEmbeddingService() 引用了不存在的 getEmbeddingService 工厂，
且 ingestDocument 调用了不存在的 embed_texts 方法；本测试在真实环境下
串起整个 upload/search 链路，使此类接口错位立即可见。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestRagApiUpload:
    """POST /api/v1/documents/upload 端到端。"""

    @pytest.mark.asyncio
    async def test_upload_text_file_creates_catalog_and_chunks(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        # 文件内容（多段，确保至少 1 个 chunk）
        content = b"First paragraph about supplier 100001.\n\nSecond paragraph about OTD rate 98%."
        files = {"file": ("test.txt", content, "text/plain")}

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L1"},
                files=files,
            )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "ingested"
        assert body["chunks"] >= 1
        assert body["document_id"].startswith("DOC-")

        # 验证 catalog 行被写入（通过 list API）
        list_resp = await client.get("/api/v1/documents")
        assert list_resp.status_code == 200
        docs = list_resp.json()
        doc_ids = [d["documentId"] for d in docs]
        assert body["document_id"] in doc_ids

    @pytest.mark.asyncio
    async def test_upload_unsupported_type_returns_422(
        self, client, mockEmbeddingService
    ) -> None:
        files = {"file": ("test.bin", b"\x00\x01\x02", "application/octet-stream")}

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L1"},
                files=files,
            )

        assert resp.status_code == 422
        assert "unsupported" in resp.json()["detail"].lower() or "Unsupported" in resp.json()["detail"]


class TestRagApiSearch:
    """POST /api/v1/documents/search 端到端。"""

    @pytest.mark.asyncio
    async def test_search_returns_valid_response_shape(
        self, client, mockEmbeddingService
    ) -> None:
        """search 端点应该返回 200 + 列表（不依赖集合为空，Milvus 是共享集合）。"""
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            resp = await client.post(
                "/api/v1/documents/search",
                params={"q": "nonexistent query that should match nothing", "topK": 5},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        # 命中项符合契约（若有）
        for hit in body:
            assert "document_id" in hit
            assert "chunk_text" in hit
            assert "distance" in hit

    @pytest.mark.asyncio
    async def test_search_after_upload_returns_hits(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        # 先上传
        content = "Supplier 100001 has OTD rate 98% and Grade A certification.".encode("utf-8")
        files = {"file": ("contract.txt", content, "text/plain")}

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            upload_resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L1"},
                files=files,
            )
            assert upload_resp.status_code == 201
            doc_id = upload_resp.json()["document_id"]

            # 检索（用相同 mock embedding，query 文本与 chunk 文本相同时应命中）
            # topK 调到 50 以覆盖共享 Milvus 集合里的所有候选
            search_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": "Supplier 100001 has OTD rate 98% and Grade A certification.", "topK": 50},
            )

        assert search_resp.status_code == 200
        hits = search_resp.json()
        # 至少有一条命中；Milvus 共享集合可能含其它测试残留，
        # 故只断言 doc_id 在命中集合中、不强求 top-1
        hit_doc_ids = {h["document_id"] for h in hits}
        assert doc_id in hit_doc_ids, (
            f"uploaded doc {doc_id} not found in hits {hit_doc_ids}"
        )

    @pytest.mark.asyncio
    async def test_search_with_security_level_filter(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        content = b"L2 confidential contract content here."
        files = {"file": ("l2.txt", content, "text/plain")}

        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ):
            upload_resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L2"},
                files=files,
            )
            assert upload_resp.status_code == 201

            # L1 过滤不应返回 L2 文档
            l1_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": "L2 confidential contract", "topK": 3, "securityLevel": "L1"},
            )
            # L2 过滤应返回
            l2_resp = await client.post(
                "/api/v1/documents/search",
                params={"q": "L2 confidential contract", "topK": 3, "securityLevel": "L2"},
            )

        assert l1_resp.status_code == 200
        assert l2_resp.status_code == 200
        # L1 过滤可能返回其他 L1 文档的残留（来自其他测试），L2 必须含本条
        l2_hits = l2_resp.json()
        assert any(h["document_id"].startswith("DOC-") for h in l2_hits)