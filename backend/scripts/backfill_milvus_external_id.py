"""M0-P0.4 (Phase 2 Task 7): backfill external_id on 3 new Milvus collections.

For each row with external_id == "" in the 3 new collections (class/property/metric):
1. Derive expected unified_id = obj:{type}:{ontology_id}
2. UPSERT PG id_mapping placeholder (ON CONFLICT DO NOTHING)
3. Delete original Milvus row (auto-id PK) + re-insert with external_id set

Idempotent: second scan finds no rows with external_id == "" → written=0.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymilvus import Collection
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.milvus_client import (
    _CLASS_COLLECTION_NAME,
    _METRIC_COLLECTION_NAME,
    _PROPERTY_COLLECTION_NAME,
    _connect,
    _connAlias,
    queryClassEmbeddings,
    queryMetricEmbeddings,
    queryPropertyEmbeddings,
)

logger = logging.getLogger(__name__)

_TYPE_TO_QUERY: dict[str, tuple[str, callable]] = {
    "class": (_CLASS_COLLECTION_NAME, queryClassEmbeddings),
    "property": (_PROPERTY_COLLECTION_NAME, queryPropertyEmbeddings),
    "metric": (_METRIC_COLLECTION_NAME, queryMetricEmbeddings),
}


def _loadAndQueryAll(name: str) -> list[dict]:
    """Load collection + query all rows with full field set (for re-insertion)."""
    collection = Collection(name, using=_connAlias())
    collection.load()
    return collection.query(
        expr="id >= 0",
        output_fields=["id", "ontology_id", "type", "name", "alias",
                       "description", "embedding", "external_id"],
        limit=16384,
    )


async def backfill(session: AsyncSession) -> int:
    """Backfill external_id for rows where it's empty. Returns rows actually written."""
    _connect()
    written = 0

    for type_, (collection_name, query_fn) in _TYPE_TO_QUERY.items():
        # Use canonical query helper first to find rows needing backfill
        rows = query_fn()
        needs_backfill = [r for r in rows if not r.get("external_id")]
        if not needs_backfill:
            logger.info("type=%s: no rows need backfill", type_)
            continue

        logger.info("type=%s: %d rows need backfill", type_, len(needs_backfill))

        # Fetch full row data for re-insertion
        full_rows = _loadAndQueryAll(collection_name)
        by_oid = {r["ontology_id"]: r for r in full_rows}

        collection = Collection(collection_name, using=_connAlias())
        collection.load()

        # Column-oriented data lists for batch insert (external_id LAST)
        col_ontology_id: list[int] = []
        col_type: list[str] = []
        col_name: list[str] = []
        col_alias: list[str] = []
        col_description: list[str] = []
        col_embedding: list[list[float]] = []
        col_external_id: list[str] = []

        for r in needs_backfill:
            ontology_id = int(r["ontology_id"])
            expected_uid = f"obj:{type_}:{ontology_id}"

            # UPSERT PG id_mapping placeholder
            sp = await session.begin_nested()
            try:
                await session.execute(
                    text(
                        "INSERT INTO id_mapping "
                        "(unified_id, business_object, external_id, milvus_collection, "
                        "milvus_id, created_time, updated_time) "
                        "VALUES (:uid, :bo, :ext, :mc, :mid, now(), now()) "
                        "ON CONFLICT (business_object, external_id) DO NOTHING"
                    ),
                    {
                        "uid": expected_uid,
                        "bo": type_,
                        "ext": str(ontology_id),
                        "mc": collection_name,
                        "mid": str(ontology_id),
                    },
                )
                await sp.commit()
            except Exception as exc:
                await sp.rollback()
                logger.warning("PG upsert failed for %s: %s", expected_uid, exc)
                continue

            # Delete original Milvus row by auto-id
            auto_id = r.get("id")
            if auto_id is not None:
                collection.delete(f"id == {auto_id}")

            # Collect column data for batch re-insert (external_id LAST)
            full = by_oid.get(ontology_id, {})
            col_ontology_id.append(ontology_id)
            col_type.append(type_)
            col_name.append(full.get("name") or "")
            col_alias.append(full.get("alias") or "")
            col_description.append(full.get("description") or "")
            col_embedding.append(full.get("embedding") or [])
            col_external_id.append(expected_uid)
            written += 1

        # Batch insert all re-filled rows (column-oriented format)
        if col_ontology_id:
            collection.insert([
                col_ontology_id,
                col_type,
                col_name,
                col_alias,
                col_description,
                col_embedding,
                col_external_id,  # external_id: LAST field
            ])
            collection.flush()
            logger.info("type=%s: backfilled %d rows", type_, len(col_ontology_id))

    await session.commit()
    return written


async def _main() -> int:
    """CLI entry: run backfill against real PG + Milvus, print summary."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from app.infrastructure.database import getSessionFactory

    factory = getSessionFactory()
    async with factory() as session:
        written = await backfill(session)
    print(f"Backfilled {written} rows across 3 collections")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
