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

# 去重超取倍数：Milvus 的 limit 按「实体」而非「不同 ontology_id」计数，集合里
# 一旦残留同一 ontology_id 的多份向量（delete-then-insert 在批量负载下不可靠，
# 见 scripts/backfill_milvus_embeddings.py --cleanup 的背景），topK 窗口就会
# 被重复项吃光——2026-09-30 线上召回事故：15 个名额被 4 个不同的类占满，
# 业务需要的类进不了候选集。故按 _DEDUP_OVERFETCH 倍超取再按 ontology_id 去重。
# 8 是经验值：历史最坏观测为每类 4 份，留一倍余量；代价是 Milvus 多算几倍邻居。
_DEDUP_OVERFETCH = 8


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
    """Search one specific collection; returns hits with keys ontology_id/type/name/alias/description/distance.

    结果按距离升序、按 ``ontology_id`` 去重（保留最近的一份），最后截断到 topK
    —— Milvus 的 ``limit`` 数的是实体，重复实体不该占调用方的 topK 名额
    （见 ``_DEDUP_OVERFETCH`` 的说明）。
    """
    collection = Collection(collectionName, using=_connAlias())
    collection.load()
    results = collection.search(
        data=[queryEmbedding],
        anns_field="embedding",
        param={"metric_type": "L2", "params": {"ef": 64}},
        limit=topK * _DEDUP_OVERFETCH,
        output_fields=["ontology_id", "type", "name", "alias", "description"],
    )
    hits: list[dict[str, Any]] = []
    seenIds: set[Any] = set()
    for result in results:
        for hit in result:
            row = {
                "ontology_id": hit.entity.get("ontology_id"),
                "type": hit.entity.get("type"),
                "name": hit.entity.get("name"),
                "alias": hit.entity.get("alias"),
                "description": hit.entity.get("description"),
                "distance": float(hit.distance),
            }
            if row["ontology_id"] in seenIds:
                continue  # 同一实体只保留最近的一份（结果已按距离升序）
            seenIds.add(row["ontology_id"])
            hits.append(row)
            if len(hits) >= topK:
                return hits
    return hits
