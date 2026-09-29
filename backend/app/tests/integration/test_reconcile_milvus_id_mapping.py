"""Milvus ↔ PG id_mapping 三方对账测试（RED）。

覆盖：
1. Milvus 有行 / PG 无映射 → 在 PG 写入占位 unified_id
2. PG 有映射 / Milvus 无行 → 警告行（不删除 PG 记录）
3. 双向都有但 unified_id 不一致 → 报错行
4. 双向一致 → 通过

依赖：真实 PG（qa_metadata_test）+ 真实 Milvus（qa-milvus:19530）+ 真实 id_mapping 表。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers (verbatim from Neo4j reconcile tests; id_mapping 表结构共享)
# ---------------------------------------------------------------------------


async def _seed_pg_id_mapping(
    session: AsyncSession,
    unified_id: str,
    entity_type: str,
    external_id: str | None = None,
    milvus_collection: str | None = None,
) -> None:
    """种入 id_mapping 测试行。external_id 默认从 unified_id 派生。"""
    ext = external_id if external_id is not None else unified_id.split(":")[-1]
    await session.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, milvus_collection, created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, :mc, now(), now()) "
            "ON CONFLICT (business_object, external_id) DO NOTHING"
        ),
        {
            "uid": unified_id,
            "bo": entity_type,
            "ext": ext,
            "mc": milvus_collection,
        },
    )
    await session.commit()


async def _truncate_id_mapping(session: AsyncSession) -> None:
    """每个测试前清空 id_mapping 表。"""
    await session.execute(text("TRUNCATE TABLE id_mapping RESTART IDENTITY CASCADE"))
    await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reconcile_reports_clean_when_both_sides_align(
    milvusSeedExternalId, dbSession: AsyncSession
):
    """两侧均一致 → reconcile exit 0，diff_count=0。"""
    from scripts.reconcile_milvus_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    # 种 3 条匹配 PG 行：business_object 决定 collection 路由
    await _seed_pg_id_mapping(
        dbSession, "obj:class:5001", "class", external_id="5001",
        milvus_collection="ontology_class_embeddings",
    )
    await _seed_pg_id_mapping(
        dbSession, "obj:property:5002", "property", external_id="5002",
        milvus_collection="ontology_property_embeddings",
    )
    await _seed_pg_id_mapping(
        dbSession, "obj:metric:5003", "metric", external_id="5003",
        milvus_collection="ontology_metric_embeddings",
    )

    report = await reconcile(dbSession)

    assert report.diff_count == 0, f"expected 0 diff, got {report.diff_count}: {report.rows}"
    assert report.exit_code == 0


async def test_reconcile_writes_placeholder_when_milvus_has_row_but_pg_missing(
    milvusSeedExternalId, dbSession: AsyncSession
):
    """Milvus 有行但 PG 无 id_mapping → reconcile 写占位行。"""
    from scripts.reconcile_milvus_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    # milvusSeedExternalId 已种 3 行带 external_id，PG 暂为空

    report = await reconcile(dbSession)

    assert report.exit_code == 0
    assert report.placeholder_count >= 1
    # 验证 PG 真有占位行（fix loop M3 教训：不仅计数，要落库可查）
    pg_uid_row = (
        await dbSession.execute(
            text("SELECT unified_id FROM id_mapping WHERE external_id = '5001'")
        )
    ).first()
    assert pg_uid_row is not None, "占位行未写入 PG"
    assert pg_uid_row[0] == "obj:class:5001"


async def test_reconcile_warns_when_pg_has_mapping_but_milvus_missing(
    milvusCleanClient, dbSession: AsyncSession
):
    """PG 有 id_mapping 但 Milvus 无对应行 → 警告行（不删 PG）。"""
    from scripts.reconcile_milvus_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    await _seed_pg_id_mapping(
        dbSession, "obj:class:GHOST", "class", external_id="GHOST",
        milvus_collection="ontology_class_embeddings",
    )

    report = await reconcile(dbSession)

    assert report.warning_count >= 1
    assert report.exit_code == 0  # 警告不致失败


async def test_reconcile_errors_when_unified_ids_mismatch(
    milvusSeedExternalId, dbSession: AsyncSession
):
    """双向都有但 unified_id 不一致 → 报错行，exit_code != 0。"""
    from scripts.reconcile_milvus_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    # Milvus 有 external_id='5001' → expected unified_id='obj:class:5001'
    # 但 PG 用 S999（不匹配）
    await _seed_pg_id_mapping(
        dbSession, "obj:class:S999", "class", external_id="5001",  # ← external_id same, unified_id different
        milvus_collection="ontology_class_embeddings",
    )

    report = await reconcile(dbSession)

    assert report.error_count >= 1
    assert report.exit_code != 0


# ---------------------------------------------------------------------------
# Backfill (v3.1 M0-P0.4: Milvus 3 collection external_id backfill)
# ---------------------------------------------------------------------------


async def test_backfill_writes_external_id_for_rows_missing_it(
    milvusSeedExternalId, dbSession: AsyncSession
):
    """Milvus rows with external_id=\"\" → backfill should populate obj:{type}:{ontology_id}."""
    from scripts.backfill_milvus_external_id import backfill

    await _truncate_id_mapping(dbSession)
    # milvusSeedExternalId seeds 3 rows (class/property/metric, ontology_id 5001-5003),
    # but _insertIntoNewCollection writes external_id="", so backfill should fill them.

    written = await backfill(dbSession)

    assert written == 3, f"expected 3 rows backfilled, got {written}"
    # Verify Milvus actually has external_id
    from app.infrastructure.milvus_client import (
        queryClassEmbeddings, queryPropertyEmbeddings, queryMetricEmbeddings,
    )
    class_rows = queryClassEmbeddings()
    assert any(r.get("external_id") == "obj:class:5001" for r in class_rows)


async def test_backfill_is_idempotent(
    milvusSeedExternalId, dbSession: AsyncSession
):
    """Running backfill twice should not write duplicate rows."""
    from scripts.backfill_milvus_external_id import backfill

    await _truncate_id_mapping(dbSession)
    written1 = await backfill(dbSession)
    written2 = await backfill(dbSession)

    assert written1 == 3
    assert written2 == 0  # Second run: no new writes


async def test_backfill_then_reconcile_yields_zero_diff(
    milvusSeedExternalId, dbSession: AsyncSession
):
    """After backfill, reconcile must report diff_count=0 (PG placeholder rows synced)."""
    from scripts.backfill_milvus_external_id import backfill
    from scripts.reconcile_milvus_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    await backfill(dbSession)
    report = await reconcile(dbSession)

    assert report.diff_count == 0, f"diff after backfill: {report.rows}"