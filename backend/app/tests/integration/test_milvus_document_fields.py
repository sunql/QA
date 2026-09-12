"""Milvus document_embeddings 定位符字段（P0 溯源地基）。

真实 Milvus + 真实集合，不 mock。
"""

from __future__ import annotations

import pytest

from app.infrastructure.milvus_client import (
    _documentFields,
    deleteDocumentChunks,
    ensureDocumentCollection,
    insertDocumentChunks,
    queryDocumentChunks,
    searchDocumentChunks,
)


class TestDocumentFields:
    def test_locator_fields_present(self) -> None:
        names = [f.name for f in _documentFields()]
        assert "page_number" in names
        assert "section_name" in names
        assert "paragraph_no" in names

    def test_field_order_matches_insert_payload(self) -> None:
        """insertDocumentChunks 用位置列表写数据，字段顺序即契约。"""
        names = [f.name for f in _documentFields()]
        assert names[-1] == "embedding", "embedding 必须最后，与 data 列表一致"
        assert names.index("page_number") < names.index("embedding")


class TestRoundTrip:
    @pytest.mark.integration
    def test_locator_survives_insert_and_search(self) -> None:
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-TEST",
                    "chunk_id": "chunk-loc",
                    "chunk_text": "供应商A暂停采购",
                    "chunk_sequence": 0,
                    "effective_date": "",
                    "security_level": "L1",
                    "page_number": 18,
                    "section_name": "质量管理",
                    "paragraph_no": 3,
                    "embedding": [0.1] * 1024,
                }
            ]
        )
        hit = _findChunk("chunk-loc", [0.1] * 1024)
        assert hit is not None, "应能检索到刚写入的 chunk"
        assert hit["page_number"] == 18
        assert hit["section_name"] == "质量管理"
        assert hit["paragraph_no"] == 3

        # 收尾：不给共享集合留残留（与本文件其它用例一致）
        deleteDocumentChunks("DOC-P0-TEST")

    @pytest.mark.integration
    def test_missing_locator_writes_sentinel(self) -> None:
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-TEST",
                    "chunk_id": "chunk-null",
                    "chunk_text": "无定位符",
                    "chunk_sequence": 1,
                    "page_number": None,
                    "section_name": None,
                    "paragraph_no": None,
                    "embedding": [0.2] * 1024,
                }
            ]
        )
        hit = _findChunk("chunk-null", [0.2] * 1024)
        assert hit is not None
        assert hit["page_number"] == -1
        assert hit["section_name"] == ""
        assert hit["paragraph_no"] == -1

        # 收尾：不给共享集合留残留
        deleteDocumentChunks("DOC-P0-TEST")


    @pytest.mark.integration
    def test_delete_document_chunks_scopes_to_one_document(self) -> None:
        """按 document_id 删除只带走该文档的 chunk，不是清空集合。"""
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-DEL",
                    "chunk_id": "chunk-del-a",
                    "chunk_text": "待删除",
                    "chunk_sequence": 0,
                    "page_number": 1,
                    "section_name": "",
                    "paragraph_no": 1,
                    "embedding": [0.3] * 1024,
                },
                {
                    "document_id": "DOC-P0-KEEP",
                    "chunk_id": "chunk-del-b",
                    "chunk_text": "不该被删",
                    "chunk_sequence": 0,
                    "page_number": 1,
                    "section_name": "",
                    "paragraph_no": 1,
                    "embedding": [0.4] * 1024,
                },
            ]
        )

        deleteDocumentChunks("DOC-P0-DEL")

        assert _findChunk("chunk-del-a", [0.3] * 1024) is None
        assert _findChunk("chunk-del-b", [0.4] * 1024) is not None

        # 收尾：本用例同样不该给共享集合留残留
        deleteDocumentChunks("DOC-P0-KEEP")

    @pytest.mark.integration
    def test_query_document_chunks_returns_locators(self) -> None:
        """按 document_id 直查（不经向量检索）也要能拿到定位符。"""
        ensureDocumentCollection()
        insertDocumentChunks(
            [
                {
                    "document_id": "DOC-P0-QRY",
                    "chunk_id": "chunk-qry",
                    "chunk_text": "直查定位符",
                    "chunk_sequence": 0,
                    "page_number": 7,
                    "section_name": "采购管理",
                    "paragraph_no": 2,
                    "embedding": [0.5] * 1024,
                }
            ]
        )

        rows = [r for r in queryDocumentChunks("DOC-P0-QRY") if r["chunk_id"] == "chunk-qry"]
        assert len(rows) == 1
        assert rows[0]["page_number"] == 7
        assert rows[0]["section_name"] == "采购管理"
        assert rows[0]["paragraph_no"] == 2

        deleteDocumentChunks("DOC-P0-QRY")


def _findChunk(chunkId: str, embedding: list[float]) -> dict | None:
    """按 chunk_id 在检索结果里定位，避免依赖排序位置。

    集合是跨用例共享的，topK=1 取到的未必是本用例刚写的那条。
    """
    for hit in searchDocumentChunks(embedding, topK=10):
        if hit["chunk_id"] == chunkId:
            return hit
    return None
