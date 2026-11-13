"""Milvus dual-write and type-routed collection management.

M0-P0.4 Phase 2 (Task 15): Extracted from milvus_client.py.

Provides:
- _classFields / _propertyFields / _metricFields / _kpiFields (schema definitions)
- ensureClassCollection / ensurePropertyCollection / ensureMetricCollection
  / ensureKpiCollection
- insertEmbeddingsDual / deleteByOntologyIdDual / _insertIntoNewCollection
"""

from __future__ import annotations

from typing import Any

from pymilvus import Collection, DataType, FieldSchema, utility

from app.infrastructure.milvus_client import (
    VALID_EMBEDDING_TYPES,
    _connAlias,
    _connect,
    _ensureCollection,
    _ontologyFields,
)


# ---------------------------------------------------------------------------
# Collection name constants (centralized in milvus_client.py; re-exported here
# for convenience so dual_write callers can import from one place)
# ---------------------------------------------------------------------------
_CLASS_COLLECTION_NAME = "ontology_class_embeddings"
_PROPERTY_COLLECTION_NAME = "ontology_property_embeddings"
_METRIC_COLLECTION_NAME = "ontology_metric_embeddings"
_KPI_COLLECTION_NAME = "kpi_catalog_embeddings"


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

def _classFields() -> list[FieldSchema]:
    """Class ontology embedding schema (includes external_id field)."""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="M0 unified_id, e.g. obj:class:1001"),
    ]


def _propertyFields() -> list[FieldSchema]:
    """Property ontology embedding schema (includes external_id field)."""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="M0 unified_id, e.g. obj:property:2001"),
    ]


def _metricFields() -> list[FieldSchema]:
    """Metric ontology embedding schema (includes external_id field)."""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="M0 unified_id, e.g. obj:metric:3001"),
    ]


def _kpiFields() -> list[FieldSchema]:
    """KPI 目录 embedding schema（与本体三集合同形 + external_id=kpi code）。"""
    return _ontologyFields() + [
        FieldSchema(name="external_id", dtype=DataType.VARCHAR, max_length=128,
                    description="kpi_catalog.kpi_code"),
    ]


# ---------------------------------------------------------------------------
# Collection ensure functions
# ---------------------------------------------------------------------------

def ensureClassCollection() -> Collection:
    """Ensure ontology_class_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_CLASS_COLLECTION_NAME, _classFields())


def ensurePropertyCollection() -> Collection:
    """Ensure ontology_property_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_PROPERTY_COLLECTION_NAME, _propertyFields())


def ensureMetricCollection() -> Collection:
    """Ensure ontology_metric_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_METRIC_COLLECTION_NAME, _metricFields())


def ensureKpiCollection() -> Collection:
    """Ensure kpi_catalog_embeddings collection exists (creates if absent)."""
    return _ensureCollection(_KPI_COLLECTION_NAME, _kpiFields())


def rebuildOntologyCollections() -> None:
    """drop 并重建 4 个 type-routed 向量集合（空集合 + embedding 索引）。

    仅 scripts/backfill_milvus_embeddings.py --cleanup 的「以 PG 为真源收敛」路径
    使用。为什么必须 drop 而不能逐条删：此部署下 Milvus 的批量 delete 不可靠
    （删除要 release+load 才可见，见 milvus_client._ensureCollection 的注释），
    残留重复行正是这么攒出来的——2026-09-30 实测 32 个类被堆成 98 行 / 3642 个
    属性被堆成 10926 行，检索 topK 窗口被重复项吃光，多步 NL2SQL 全线失败。

    传空集合 + 索引保证后续 insert 直接可用；集合不存在时不 drop（首次运行幂等）。
    """
    _connect()
    collections = (
        (_CLASS_COLLECTION_NAME, ensureClassCollection),
        (_PROPERTY_COLLECTION_NAME, ensurePropertyCollection),
        (_METRIC_COLLECTION_NAME, ensureMetricCollection),
        (_KPI_COLLECTION_NAME, ensureKpiCollection),
    )
    for name, _ in collections:
        if utility.has_collection(name, using=_connAlias()):
            utility.drop_collection(name, using=_connAlias())
    # drop 全部走完再建：避免「同名重建」与尚未传播的 drop 抢跑
    for _, ensure in collections:
        ensure()


# ---------------------------------------------------------------------------
# Dual-write insert
# ---------------------------------------------------------------------------

def insertEmbeddingsDual(records: list[dict[str, Any]]) -> None:
    """Dual-write: insert into type-routed new collection.

    Per-record routing: ``type='class'`` → ontology_class_embeddings, etc.
    Each new collection's ``external_id`` field carries the record's optional
    ``external_id`` (KPI 行为 kpi_catalog.kpi_code)；缺省为空字符串，本体行由
    backfill (Task M7) 回填。

    Task 14 (M12-drop): ontology_embeddings 已删除；旧 collection leg 已移除（原
    ``insertEmbeddings(records)`` 调用会因 collection 缺席抛错；现在直接走 type-routed）。

    Raises:
        ValueError: if any record has invalid ``type`` (not in VALID_EMBEDDING_TYPES).
    """
    if not records:
        return

    # Validate types
    for r in records:
        type_ = r.get("type")
        if type_ not in VALID_EMBEDDING_TYPES:
            raise ValueError(f"Invalid type {type_!r}; must be one of {sorted(VALID_EMBEDDING_TYPES)}")

    # New collection write: route by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_type.setdefault(r["type"], []).append(r)

    if "class" in by_type:
        _insertIntoNewCollection(_CLASS_COLLECTION_NAME, by_type["class"])
    if "property" in by_type:
        _insertIntoNewCollection(_PROPERTY_COLLECTION_NAME, by_type["property"])
    if "metric" in by_type:
        _insertIntoNewCollection(_METRIC_COLLECTION_NAME, by_type["metric"])
    if "kpi" in by_type:
        _insertIntoNewCollection(_KPI_COLLECTION_NAME, by_type["kpi"])


def _insertIntoNewCollection(name: str, records: list[dict[str, Any]]) -> None:
    """Insert records into a new type-routed (class/property/metric/kpi) variant.

    Same schema as existing insertEmbeddings PLUS the new external_id field
    (透传记录上的可选 external_id；缺省 ""，本体行由 Task M7 backfill 回填）。
    """
    import logging
    logger = logging.getLogger(__name__)

    if name == _CLASS_COLLECTION_NAME:
        collection = ensureClassCollection()
    elif name == _PROPERTY_COLLECTION_NAME:
        collection = ensurePropertyCollection()
    elif name == _METRIC_COLLECTION_NAME:
        collection = ensureMetricCollection()
    elif name == _KPI_COLLECTION_NAME:
        collection = ensureKpiCollection()
    else:
        raise ValueError(f"Unknown collection name: {name}")

    data = [
        [r["ontology_id"] for r in records],       # id is auto_id, skip
        [r["type"] for r in records],
        [r["name"] for r in records],
        [r.get("alias") or "" for r in records],
        [r.get("description") or "" for r in records],
        [r["embedding"] for r in records],         # embedding before external_id
        [r.get("external_id") or "" for r in records],  # external_id: kpi=kpi_code；本体行缺省 ""
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Dual-write: inserted %d embeddings into %s", len(records), name)


# ---------------------------------------------------------------------------
# Dual-write delete
# ---------------------------------------------------------------------------

def deleteByOntologyIdDual(ontologyId: int, type: str) -> None:
    """Delete from type-routed new collection (Task 14: ontology_embeddings 已 drop)。

    历史背景：双写窗口期必须双删；Task 14 之后旧 collection 不存在，dual 删除
    退化为 type-routed collection 单独删除（原 ``deleteByOntologyId(ontologyId, type)``
    调用因 collection 缺席无效，已移除）。

    type 作用域理由同 deleteByOntologyId：ontology_id 跨类型不唯一。
    """
    import logging
    logger = logging.getLogger(__name__)

    if type not in VALID_EMBEDDING_TYPES:
        raise ValueError(f"unknown embedding type: {type!r}")

    # Type-routed new collection
    name_to_ensure = {
        "class": ensureClassCollection,
        "property": ensurePropertyCollection,
        "metric": ensureMetricCollection,
        "kpi": ensureKpiCollection,
    }[type]
    new_collection = name_to_ensure()
    new_collection.delete(f"ontology_id == {ontologyId}")
    new_collection.flush()
    logger.info(
        "Deleted Milvus records for ontology_id=%d type=%s",
        ontologyId, type,
    )
