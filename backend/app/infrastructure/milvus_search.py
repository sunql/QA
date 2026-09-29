"""Milvus search for type-routed 3-collection embeddings.

M0-P0.4 Phase 2 (Task 15): Extracted from milvus_client.py.

Provides:
- searchEmbeddingsByTypeRouted / _searchCollection
"""

from __future__ import annotations

from typing import Any

from pymilvus import Collection

from app.infrastructure.milvus_client import (
    VALID_EMBEDDING_TYPES,
    _connAlias,
)


# ---------------------------------------------------------------------------
# Collection name constants (imported from milvus_client.py for centralization)
# ---------------------------------------------------------------------------
_CLASS_COLLECTION_NAME = "ontology_class_embeddings"
_PROPERTY_COLLECTION_NAME = "ontology_property_embeddings"
_METRIC_COLLECTION_NAME = "ontology_metric_embeddings"


# ---------------------------------------------------------------------------
# Public search API
# ---------------------------------------------------------------------------

def searchEmbeddingsByTypeRouted(
    queryEmbedding: list[float],
    topK: int = 5,
    typeFilter: str | None = None,
) -> list[dict[str, Any]]:
    """向量相似度搜索（type-routed；走 3 个 type-specific collection）。

    Args:
        queryEmbedding: 查询向量
        topK: 返回条数
        typeFilter: 可选，限定类型（class/property/metric）

    Returns:
        匹配的记录列表（含 ontology_id / type / name / alias / description / distance），
        按 distance 升序。

    Notes:
        - typeFilter is None → 跨 3 collection 各取 topK，合并排序取 topK
        - typeFilter == "class" → 只查 ontology_class_embeddings
        - typeFilter == "property" → 只查 ontology_property_embeddings
        - typeFilter == "metric" → 只查 ontology_metric_embeddings
    """
    if typeFilter is not None and typeFilter not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type filter: {typeFilter!r}")

    if typeFilter == "class":
        return _searchCollection(_CLASS_COLLECTION_NAME, queryEmbedding, topK)
    if typeFilter == "property":
        return _searchCollection(_PROPERTY_COLLECTION_NAME, queryEmbedding, topK)
    if typeFilter == "metric":
        return _searchCollection(_METRIC_COLLECTION_NAME, queryEmbedding, topK)

    # typeFilter is None: search all 3 collections, merge, sort by distance, take topK
    perCollectionTopK = topK
    perHits: list[dict[str, Any]] = []
    for collectionName in (
        _CLASS_COLLECTION_NAME,
        _PROPERTY_COLLECTION_NAME,
        _METRIC_COLLECTION_NAME,
    ):
        perHits.extend(_searchCollection(collectionName, queryEmbedding, perCollectionTopK))

    perHits.sort(key=lambda h: h["distance"])
    return perHits[:topK]


def _searchCollection(
    collectionName: str,
    queryEmbedding: list[float],
    topK: int,
) -> list[dict[str, Any]]:
    """Search one specific collection; returns hits with keys ontology_id/type/name/alias/description/distance."""
    collection = Collection(collectionName, using=_connAlias())
    collection.load()
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"ef": 64}},
        limit=topK,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
    )
    hits: list[dict[str, Any]] = []
    for result in results:
        for hit in result:
            hits.append({
                "ontology_id": hit.entity.get("ontology_id"),
                "type": hit.entity.get("type"),
                "name": hit.entity.get("name"),
                "alias": hit.entity.get("alias"),
                "description": hit.entity.get("description"),
                "distance": float(hit.distance),
            })
    return hits
