"""孤儿文档向量清理：集合差（纯函数）。"""

from __future__ import annotations

from scripts.purge_orphan_document_vectors import orphanDocumentIds


def test_orphans_are_milvus_only_ids_sorted() -> None:
    orphan = orphanDocumentIds(
        milvusIds={"DOC-3", "DOC-1", "DOC-2"},
        catalogIds={"DOC-2", "DOC-9"},
    )

    assert orphan == ["DOC-1", "DOC-3"]


def test_no_orphans_when_catalog_covers_milvus() -> None:
    assert orphanDocumentIds({"A"}, {"A", "B"}) == []


def test_empty_milvus_means_no_orphans() -> None:
    assert orphanDocumentIds(set(), {"A"}) == []
