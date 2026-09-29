"""M0-P0.4 (Phase 2 Task 5): migrate ontology_embeddings → 3 type-specific collections.

数据迁移：
- Read all rows from old ontology_embeddings.
- Group by type field (class/property/metric).
- Insert each group into the appropriate new collection.
- Idempotent: skip rows whose ontology_id already exists in the target collection.

⚠️ 真实数据库执行：直接写 Milvus，谨慎使用。
"""
from __future__ import annotations

import logging
from typing import Any

from pymilvus import Collection, utility

from app.infrastructure.milvus_client import (
    _CLASS_COLLECTION_NAME,
    _METRIC_COLLECTION_NAME,
    _PROPERTY_COLLECTION_NAME,
    _connAlias,
    _connect,
    ensureClassCollection,
    ensureMetricCollection,
    ensurePropertyCollection,
    listAllEmbeddings,
)

logger = logging.getLogger(__name__)

_TYPE_TO_COLLECTION: dict[str, str] = {
    "class": _CLASS_COLLECTION_NAME,
    "property": _PROPERTY_COLLECTION_NAME,
    "metric": _METRIC_COLLECTION_NAME,
}

_TYPE_TO_ENSURE: dict[str, Any] = {
    _CLASS_COLLECTION_NAME: ensureClassCollection,
    _PROPERTY_COLLECTION_NAME: ensurePropertyCollection,
    _METRIC_COLLECTION_NAME: ensureMetricCollection,
}


def _existingOntologyIds(collectionName: str) -> set[int]:
    """返回目标 collection 已存在的 ontology_id 集合（用于幂等跳过）。

    集合不存在时返回空集（首次迁移场景）。
    """
    if not utility.has_collection(collectionName, using=_connAlias()):
        return set()
    collection = Collection(collectionName, using=_connAlias())
    collection.load()
    rows = collection.query(expr="id >= 0", output_fields=["ontology_id"], limit=16384)
    return {int(r["ontology_id"]) for r in rows}


def _insertIntoCollection(collectionName: str, records: list[dict[str, Any]]) -> int:
    """Insert records into target collection. external_id defaults to "".

    Returns count of actually-inserted records.

    Order MUST match schema of new 3 collections:
    ``_ontologyFields() + [external_id]`` =
    ``[id(auto), ontology_id, type, name, alias, description, embedding, external_id]``
    So positional insert (skipping auto id) is:
    ``[ontology_id, type, name, alias, description, embedding, external_id]``.
    """
    if not records:
        return 0

    # Use ensure helper so the script works against fresh Milvus (no M2-created
    # collections yet). Matches canonical pattern in milvus_client._insertIntoNewCollection.
    ensureFn = _TYPE_TO_ENSURE.get(collectionName)
    if ensureFn is None:
        raise ValueError(f"Unknown new-collection name: {collectionName}")
    collection = ensureFn()
    collection.load()

    data = [
        [r["ontology_id"] for r in records],
        [r["type"] for r in records],
        [r["name"] for r in records],
        [r.get("alias") or "" for r in records],
        [r.get("description") or "" for r in records],
        [r["embedding"] for r in records],
        ["" for _ in records],  # external_id: empty (backfilled by Task M7)
    ]
    collection.insert(data)
    collection.flush()
    logger.info("Migrated %d rows into %s", len(records), collectionName)
    return len(records)


def migrate() -> dict[str, int]:
    """主入口。返回每个新 collection 的写入计数。

    流程：
    1. 读取旧 ontology_embeddings 全量。
    2. 按 type 分组。
    3. 对每组，过滤掉目标 collection 已存在的 ontology_id。
    4. 写入目标 collection。
    5. 返回 {collection_name: inserted_count} 字典。
    """
    _connect()
    rows = listAllEmbeddings()
    logger.info("Read %d rows from old ontology_embeddings", len(rows))

    # Group by type
    byType: dict[str, list[dict[str, Any]]] = {"class": [], "property": [], "metric": []}
    for r in rows:
        t = r.get("type")
        if t in byType:
            byType[t].append(r)
        else:
            logger.warning(
                "Skipping row with unknown type=%r (ontology_id=%s)", t, r.get("ontology_id")
            )

    counts: dict[str, int] = {}
    for type_, typeRows in byType.items():
        target = _TYPE_TO_COLLECTION[type_]
        existing = _existingOntologyIds(target)
        toInsert = [r for r in typeRows if int(r["ontology_id"]) not in existing]
        skipped = len(typeRows) - len(toInsert)
        logger.info(
            "type=%s: %d total, %d to insert, %d already exist",
            type_,
            len(typeRows),
            len(toInsert),
            skipped,
        )
        counts[target] = _insertIntoCollection(target, toInsert)

    return counts


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    counts = migrate()
    total = sum(counts.values())
    print("\n=== Migration Summary ===")
    for name, count in counts.items():
        print(f"  {name}: {count} inserted")
    print(f"  Total: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
