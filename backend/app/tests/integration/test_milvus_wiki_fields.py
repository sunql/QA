"""Milvus wiki_page_embeddings 字段契约 + 往返（feat-wiki-semantic-search）。

字段契约部分不依赖 Milvus（纯 schema 断言）；RoundTrip 部分真实 Milvus。
"""

from __future__ import annotations

import pytest

from app.infrastructure.milvus_client import (
    _wikiPageFields,
    deleteWikiPageChunks,
    ensureWikiPageCollection,
    insertWikiPageChunks,
    queryWikiPageChunks,
    searchWikiPageChunks,
)


class TestWikiPageFields:
    def test_metadata_fields_present(self) -> None:
        names = [f.name for f in _wikiPageFields()]
        for expected in ("page_id", "chunk_id", "chunk_text", "chunk_sequence", "title", "dimension", "status"):
            assert expected in names

    def test_field_order_matches_insert_payload(self) -> None:
        """insertWikiPageChunks 用位置列表写数据，字段顺序即契约。"""
        names = [f.name for f in _wikiPageFields()]
        assert names[0] == "id", "auto_id 主键在最前，不出现在 data 列表"
        assert names[-1] == "embedding", "embedding 必须最后，与 data 列表一致"
        assert names.index("page_id") < names.index("chunk_id") < names.index("embedding")


class TestRoundTrip:
    @pytest.mark.integration
    def test_metadata_survives_insert_and_search(self) -> None:
        ensureWikiPageCollection()
        insertWikiPageChunks(
            [
                {
                    "page_id": "PAGE-WIKI-VEC-TEST",
                    "chunk_id": "wc-0",
                    "chunk_text": "供应商准入门槛",
                    "chunk_sequence": 0,
                    "title": "准入规则",
                    "dimension": "RULE",
                    "status": "EFFECTIVE",
                    "embedding": [0.1] * 1024,
                }
            ]
        )
        hits = searchWikiPageChunks([0.1] * 1024, topK=10)
        hit = next((h for h in hits if h["chunk_id"] == "wc-0"), None)
        assert hit is not None
        assert hit["page_id"] == "PAGE-WIKI-VEC-TEST"
        assert hit["title"] == "准入规则"
        assert hit["dimension"] == "RULE"
        assert hit["status"] == "EFFECTIVE"
        assert hit["distance"] >= 0.0

        deleteWikiPageChunks("PAGE-WIKI-VEC-TEST")

    @pytest.mark.integration
    def test_status_exclude_filters_expired(self) -> None:
        ensureWikiPageCollection()
        insertWikiPageChunks(
            [
                {
                    "page_id": "PAGE-WIKI-VEC-EXP",
                    "chunk_id": "wc-exp",
                    "chunk_text": "已过期条目",
                    "chunk_sequence": 0,
                    "title": "过期",
                    "dimension": "RULE",
                    "status": "EXPIRED",
                    "embedding": [0.2] * 1024,
                }
            ]
        )
        hits = searchWikiPageChunks([0.2] * 1024, topK=50)
        assert all(h["page_id"] != "PAGE-WIKI-VEC-EXP" for h in hits), (
            "EXPIRED 默认被排除"
        )
        # 显式不过滤时能查到
        hitsAll = searchWikiPageChunks([0.2] * 1024, statusExclude=(), topK=50)
        assert any(h["chunk_id"] == "wc-exp" for h in hitsAll)

        deleteWikiPageChunks("PAGE-WIKI-VEC-EXP")

    @pytest.mark.integration
    def test_delete_scopes_to_one_page(self) -> None:
        ensureWikiPageCollection()
        insertWikiPageChunks(
            [
                {
                    "page_id": "PAGE-WIKI-VEC-DEL",
                    "chunk_id": "wc-del",
                    "chunk_text": "待删除",
                    "chunk_sequence": 0,
                    "title": "",
                    "dimension": "",
                    "status": "DRAFT",
                    "embedding": [0.3] * 1024,
                },
                {
                    "page_id": "PAGE-WIKI-VEC-KEEP",
                    "chunk_id": "wc-keep",
                    "chunk_text": "不该被删",
                    "chunk_sequence": 0,
                    "title": "",
                    "dimension": "",
                    "status": "DRAFT",
                    "embedding": [0.4] * 1024,
                },
            ]
        )

        deleteWikiPageChunks("PAGE-WIKI-VEC-DEL")

        assert not queryWikiPageChunks("PAGE-WIKI-VEC-DEL")
        assert any(
            r["chunk_id"] == "wc-keep" for r in queryWikiPageChunks("PAGE-WIKI-VEC-KEEP")
        )

        deleteWikiPageChunks("PAGE-WIKI-VEC-KEEP")
