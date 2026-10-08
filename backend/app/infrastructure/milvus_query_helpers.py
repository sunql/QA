"""Milvus query helpers for type-routed 3-collection reads.

M0-P0.4 Phase 2 (Task 15): Extracted from milvus_client.py.

Provides:
- queryClassEmbeddings / queryPropertyEmbeddings / queryMetricEmbeddings
- listEmbeddingsAcross3Collections / _queryAllRowsFromCollection
"""

from __future__ import annotations

from typing import Any

from pymilvus import Collection

from app.infrastructure.milvus_client import (
    _MILVUS_QUERY_PAGE,
    _connAlias,
    _connect,
)


# ---------------------------------------------------------------------------
# Collection name constants (imported from milvus_client.py for centralization)
# ---------------------------------------------------------------------------
_CLASS_COLLECTION_NAME = "ontology_class_embeddings"
_PROPERTY_COLLECTION_NAME = "ontology_property_embeddings"
_METRIC_COLLECTION_NAME = "ontology_metric_embeddings"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensureCollectionByName(name: str) -> Collection:
    """Connect and get Collection handle by name (assumes collection already exists)."""
    _connect()
    from pymilvus import Collection
    return Collection(name, using=_connAlias())


def _queryAllRowsFromCollection(name: str) -> list[dict[str, Any]]:
    """Full query of a collection, returning list[dict].

    Uses simple query(limit=_MILVUS_QUERY_PAGE) since each new collection
    stays well under 16384 rows in production. For future scale, swap to
    query_iterator like _queryAllRows does.

    字段投影含 alias/description（不含 1024 维 embedding）：cleanup 的
    「读全量 → 去重 → 删集重建」要靠 alias/description 写回，缺一则 drop 之后
    KeyError 崩在半路（2026-09-30 修复，见 test_milvus_query_projection.py）。
    """
    collection = _ensureCollectionByName(name)
    collection.load()
    results = collection.query(
        expr="id >= 0",
        output_fields=[
            "ontology_id", "type", "name", "alias", "description", "external_id",
        ],
        limit=_MILVUS_QUERY_PAGE,
    )
    return results


# ---------------------------------------------------------------------------
# Per-type query functions
# ---------------------------------------------------------------------------

def queryClassEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from ontology_class_embeddings."""
    return _queryAllRowsFromCollection(_CLASS_COLLECTION_NAME)


def queryPropertyEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from ontology_property_embeddings."""
    return _queryAllRowsFromCollection(_PROPERTY_COLLECTION_NAME)


def queryMetricEmbeddings() -> list[dict[str, Any]]:
    """Read all rows from ontology_metric_embeddings."""
    return _queryAllRowsFromCollection(_METRIC_COLLECTION_NAME)


# ---------------------------------------------------------------------------
# Cross-collection read
# ---------------------------------------------------------------------------

def listEmbeddingsAcross3Collections() -> list[dict[str, Any]]:
    """读取 3 个新 type-specific collection 全量，合并返回。

    与 listAllEmbeddings() 的差别：读 ontology_class_embeddings /
    ontology_property_embeddings / ontology_metric_embeddings，而非旧
    ontology_embeddings。M7 backfill 后 3 个新 collection 数据与旧一致；
    M12+ 旧 collection 丢弃后，此函数成为唯一读取入口。

    适用：诊断/统计 API（vectors.py 等）。
    """
    _connect()
    rows: list[dict[str, Any]] = []
    for query_fn in (
        queryClassEmbeddings,
        queryPropertyEmbeddings,
        queryMetricEmbeddings,
    ):
        rows.extend(query_fn())
    return rows
