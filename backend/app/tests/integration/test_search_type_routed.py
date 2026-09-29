"""M0-P0.4 M12-prep: type-routed searchEmbeddingsByTypeRouted (RED → GREEN).

Real Milvus integration test: searchEmbeddingsByTypeRouted must route to
the 3 type-specific collections (class/property/metric), preserve the
old searchByEmbedding output schema, and merge results when typeFilter=None.

Seeds 1 row per type into the 3 new collections via insertEmbeddingsDual.
Embeddings are orthogonal basis vectors for deterministic L2 distances.
"""
from __future__ import annotations

import pytest

from app.infrastructure.milvus_client import (
    insertEmbeddingsDual,
    searchEmbeddingsByTypeRouted,
)


_EMBEDDING_DIM = 1024


def _makeOrthogonalBasisVector(coordinate: int) -> list[float]:
    """Build a 1024-dim unit vector with 1.0 at the given coordinate, 0.0 elsewhere.

    Coordinate must be in [0, 1023]. Using orthogonal basis vectors gives
    deterministic L2 distances:
      - distance(v, v) = 0
      - distance(v, w) = sqrt(2) for coordinate(v) != coordinate(w)
    """
    assert 0 <= coordinate < _EMBEDDING_DIM
    vec = [0.0] * _EMBEDDING_DIM
    vec[coordinate] = 1.0
    return vec


def _seedOneRowPerType(milvusCleanClient) -> None:
    """Seed 1 class + 1 property + 1 metric row into the 3 new collections.

    Uses orthogonal basis vectors so query==class_vec yields distance 0 for
    the class row and distance sqrt(2) for the property/metric rows.
    insertEmbeddingsDual writes to BOTH old and new collections (M3 dual-write).
    """
    insertEmbeddingsDual([
        {"ontology_id": 2001, "type": "class", "name": "Class1",
         "alias": "", "description": "", "embedding": _makeOrthogonalBasisVector(0)},
        {"ontology_id": 2002, "type": "property", "name": "Property1",
         "alias": "", "description": "", "embedding": _makeOrthogonalBasisVector(1)},
        {"ontology_id": 2003, "type": "metric", "name": "Metric1",
         "alias": "", "description": "", "embedding": _makeOrthogonalBasisVector(2)},
    ])


@pytest.mark.integration
def test_search_type_routed_class_filter_returns_only_class_rows(milvusCleanClient):
    """typeFilter='class' must hit only ontology_class_embeddings."""
    _seedOneRowPerType(milvusCleanClient)

    queryVec = _makeOrthogonalBasisVector(0)  # == class row's embedding
    hits = searchEmbeddingsByTypeRouted(queryVec, topK=5, typeFilter="class")

    assert all(h["type"] == "class" for h in hits)
    assert len(hits) <= 1  # only one class row exists
    assert hits[0]["ontology_id"] == 2001
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.integration
def test_search_type_routed_property_filter_returns_only_property_rows(milvusCleanClient):
    """typeFilter='property' must hit only ontology_property_embeddings."""
    _seedOneRowPerType(milvusCleanClient)

    queryVec = _makeOrthogonalBasisVector(0)
    hits = searchEmbeddingsByTypeRouted(queryVec, topK=5, typeFilter="property")

    assert all(h["type"] == "property" for h in hits)
    assert len(hits) <= 1  # only one property row exists
    assert hits[0]["ontology_id"] == 2002


@pytest.mark.integration
def test_search_type_routed_no_filter_merges_3_collections(milvusCleanClient):
    """typeFilter=None must search all 3 collections and merge by distance."""
    _seedOneRowPerType(milvusCleanClient)

    queryVec = _makeOrthogonalBasisVector(0)
    hits = searchEmbeddingsByTypeRouted(queryVec, topK=5, typeFilter=None)

    # 1 row from each collection → at least one hit per type, at most 3 total
    assert len(hits) <= 3
    typesSeen = {h["type"] for h in hits}
    assert typesSeen == {"class", "property", "metric"}
    # class row distance == 0, must come first
    assert hits[0]["type"] == "class"
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-6)
    assert hits[0]["ontology_id"] == 2001