"""M0-P0.4: dual-write insertEmbeddingsDual (RED → GREEN).

Real Milvus integration test: row must appear in BOTH old ontology_embeddings
AND the type-routed new collection.
"""
from __future__ import annotations

import pytest

from app.infrastructure.milvus_client import (
    _CLASS_COLLECTION_NAME,
    _COLLECTION_NAME,
    insertEmbeddingsDual,
    listAllEmbeddings,
    queryClassEmbeddings,
)


@pytest.mark.integration
def test_dual_write_inserts_to_old_and_new_collection(milvusCleanClient):
    """A type='class' record must appear in both ontology_embeddings and ontology_class_embeddings."""
    insertEmbeddingsDual([
        {"ontology_id": 5001, "type": "class", "name": "DualWrite", "alias": "", "description": "", "embedding": [0.5] * 1024},
    ])

    # Old collection: should have the row
    old_rows = listAllEmbeddings()
    assert any(r.get("ontology_id") == 5001 for r in old_rows)

    # New class collection: should also have the row
    new_rows = queryClassEmbeddings()
    assert any(r.get("ontology_id") == 5001 for r in new_rows)
