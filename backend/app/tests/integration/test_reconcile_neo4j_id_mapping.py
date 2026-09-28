"""Neo4j ↔ PG id_mapping 三方对账测试（RED）。

覆盖：
1. Neo4j 有节点 / PG 无映射 → 在 PG 写入占位 unified_id
2. PG 有映射 / Neo4j 无节点 → 警告行（不删除 PG 记录）
3. 双向都有但 unified_id 不一致 → 报错行
4. 双向一致 → 通过

依赖：真实 PG（qa_metadata_test）+ 真实 Neo4j（qa-neo4j:7687）+ 真实 id_mapping 表。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _seed_pg_id_mapping(session: AsyncSession, unified_id: str, entity_type: str) -> None:
    """种入 id_mapping 测试行。"""
    await session.execute(
        text(
            "INSERT INTO id_mapping "
            "(unified_id, business_object, external_id, created_time, updated_time) "
            "VALUES (:uid, :bo, :ext, now(), now()) "
            "ON CONFLICT (business_object, external_id) DO NOTHING"
        ),
        {"uid": unified_id, "bo": entity_type, "ext": unified_id.split(":")[-1]},
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
    neo4jSeedClasses, dbSession: AsyncSession
):
    """两侧均一致 → reconcile exit 0，diff_count=0。"""
    from scripts.reconcile_neo4j_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    await _seed_pg_id_mapping(dbSession, "obj:supplier:S001", "supplier")

    report = await reconcile(dbSession, neo4jSeedClasses)

    assert report.diff_count == 0, f"expected 0 diff, got {report.diff_count}"
    assert report.exit_code == 0


async def test_reconcile_writes_placeholder_when_neo4j_has_node_but_pg_missing(
    neo4jSeedClasses, dbSession: AsyncSession
):
    """Neo4j 有 Class 节点但 PG 无 id_mapping → reconcile 写占位行。"""
    from scripts.reconcile_neo4j_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    # neo4jSeedClasses 已种入 obj:supplier:S001 + 物料/客户（无 unified_id）

    report = await reconcile(dbSession, neo4jSeedClasses)

    assert report.exit_code == 0
    assert report.placeholder_count >= 1


async def test_reconcile_warns_when_pg_has_mapping_but_neo4j_missing(
    neo4jCleanDriver, dbSession: AsyncSession
):
    """PG 有 id_mapping 但 Neo4j 无节点 → 警告行（不删 PG）。"""
    from scripts.reconcile_neo4j_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    await _seed_pg_id_mapping(dbSession, "obj:supplier:GHOST", "supplier")

    report = await reconcile(dbSession, neo4jCleanDriver)

    assert report.warning_count >= 1
    assert report.exit_code == 0  # 警告不致失败


async def test_reconcile_errors_when_unified_ids_mismatch(
    neo4jSeedClasses, dbSession: AsyncSession
):
    """双向都有但 unified_id 不一致 → 报错行，exit_code != 0。"""
    from scripts.reconcile_neo4j_id_mapping import reconcile

    await _truncate_id_mapping(dbSession)
    # Neo4j 已种入 obj:supplier:S001，但 PG 用 S999
    await _seed_pg_id_mapping(dbSession, "obj:supplier:S999", "supplier")

    report = await reconcile(dbSession, neo4jSeedClasses)

    assert report.error_count >= 1
    assert report.exit_code != 0
