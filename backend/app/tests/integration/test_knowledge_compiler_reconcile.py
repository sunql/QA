"""KnowledgeCompilerService.reconcile 集成测试（v3.1 任务 A5）。

真实 PG（qa_metadata_test）上的只读对账巡检：
- 三库 count 聚合（PG 真库；Neo4j / Milvus 连接边界 fake —— 两库自身的集成
  已在各自既有测试覆盖，此处验证 reconcile 编排 + audit 落库 + 只读性）
- 不一致 → warning；一致 → 空库 diff=0
- 结果落 audit_history（audit_log）
- 只读：reconcile 前后 PG 业务行数不变（audit_log 除外）
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.knowledge_compiler_service as kc_module
from app.domain.models import AuditLog, OntologyClass
from app.services.knowledge_compiler_service import KnowledgeCompilerService


async def _fakeExternalCounts(
    monkeypatch: pytest.MonkeyPatch, neo4jCounts: dict, milvusCounts: dict
) -> None:
    """在模块边界 fake Neo4j / Milvus count 源（PG 走真库）。"""

    def fakeCountNeo4j() -> dict:
        return dict(neo4jCounts)

    def fakeCountMilvus() -> dict:
        return dict(milvusCounts)

    monkeypatch.setattr(
        kc_module.KnowledgeCompilerService, "_countNeo4j", staticmethod(fakeCountNeo4j)
    )
    monkeypatch.setattr(
        kc_module.KnowledgeCompilerService, "_countMilvus", staticmethod(fakeCountMilvus)
    )


async def _ontologyClassCount(dbSession: AsyncSession) -> int:
    return (
        await dbSession.execute(select(func.count(OntologyClass.id)))
    ).scalar_one()


@pytest.mark.asyncio
async def test_reconcile_real_pg_empty_diff_zero_writes_audit(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空测试库 + 三库一致 → diff=0、isConsistent、audit 落一行。"""
    await _fakeExternalCounts(
        monkeypatch,
        {"class": 0, "property": 0, "metric": 0},
        {"class": 0, "property": 0, "metric": 0},
    )
    svc = KnowledgeCompilerService()
    summary = await svc.reconcile(dbSession)

    assert summary.pg_counts == {"class": 0, "property": 0, "metric": 0}
    assert summary.isConsistent is True
    assert summary.warnings == ()

    rows = (
        await dbSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "knowledge_compile_reconcile"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    payload = rows[0].after_json or {}
    # after 快照里能还原三库 count（字段名以 toDict 契约为准）
    assert "pg_counts" in payload


@pytest.mark.asyncio
async def test_reconcile_detects_mismatch_against_real_pg(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PG 真库有 2 行 class、fake Neo4j 只有 1 → warning + isConsistent=False。"""
    dbSession.add(OntologyClass(class_name="供应商"))
    dbSession.add(OntologyClass(class_name="物料"))
    await dbSession.commit()

    await _fakeExternalCounts(
        monkeypatch,
        {"class": 1, "property": 0, "metric": 0},
        {"class": 2, "property": 0, "metric": 0},
    )
    svc = KnowledgeCompilerService()
    summary = await svc.reconcile(dbSession)

    assert summary.pg_counts["class"] == 2
    assert summary.isConsistent is False
    assert any("class" in w for w in summary.warnings)


@pytest.mark.asyncio
async def test_reconcile_is_read_only_on_pg(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读巡检：reconcile 前后业务行数不变（audit_log 除外）。"""
    dbSession.add(OntologyClass(class_name="供应商"))
    await dbSession.commit()
    before = await _ontologyClassCount(dbSession)

    await _fakeExternalCounts(
        monkeypatch,
        {"class": 0, "property": 0, "metric": 0},
        {"class": 0, "property": 0, "metric": 0},
    )
    svc = KnowledgeCompilerService()
    first = await svc.reconcile(dbSession)
    second = await svc.reconcile(dbSession)

    await dbSession.rollback()
    after = await _ontologyClassCount(dbSession)
    assert before == after == 1, "reconcile 不得改动 PG 业务数据"
    # 幂等重跑：两次结果一致（不产生漂移）
    assert first.pg_counts == second.pg_counts
    assert first.isConsistent == second.isConsistent
