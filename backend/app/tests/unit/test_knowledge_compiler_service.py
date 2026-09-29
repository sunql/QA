"""KnowledgeCompilerService 门面单测（v3.1 任务 A5）。

覆盖（brief 硬约束 1：unit 测门面分发/聚合/失败隔离）：
- parseUnifiedId：合法格式 + 非法格式 fail-fast
- compileObject：三角色分发 + 聚合（frozen dataclass）
- 失败隔离：单角色失败不拖垮其他角色；Milvus 断连（fake 抛错）→ vector failed
  且 PG 无半成品（本体行原样）
- reconcile：三库 count 聚合、不一致 warning、audit_history 落库（真实 PG audit_log）
- 调度纯函数：nextReconcileRun / isReconcileDue（croniter，同 agent_scheduler 模式）

外部库（Neo4j / Milvus / embedding）一律 fake —— 它们自身的集成在各自既有测试覆盖。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.knowledge_compiler_service as kc_module
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import (
    AuditLog,
    IdMapping,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.services.knowledge_compiler_service import (
    CompileObjectResult,
    CompileRoleResult,
    KnowledgeCompilerService,
    ReconcileSummary,
    parseUnifiedId,
)
from app.services.kpi_match_cache import get_kpi_match_cache

_NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# parseUnifiedId
# ---------------------------------------------------------------------------


def test_parse_unified_id_accepts_supported_types() -> None:
    assert parseUnifiedId("obj:class:12") == ("class", 12)
    assert parseUnifiedId("obj:property:7") == ("property", 7)
    assert parseUnifiedId("obj:metric:99") == ("metric", 99)


@pytest.mark.parametrize(
    "bad", ["", "class:12", "obj:class:abc", "obj:widget:12", "obj:class", "obj:class:1:extra"]
)
def test_parse_unified_id_rejects_bad_format(bad: str) -> None:
    with pytest.raises(ValidationError):
        parseUnifiedId(bad)


# ---------------------------------------------------------------------------
# compileObject：分发 + 聚合
# ---------------------------------------------------------------------------


async def _seedClass(session: AsyncSession) -> OntologyClass:
    entity = OntologyClass(class_name="供应商", class_alias="supplier")
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    return entity


async def test_compile_object_dispatches_all_three_roles(dbSession: AsyncSession) -> None:
    entity = await _seedClass(dbSession)
    svc = KnowledgeCompilerService()

    calls: list[str] = []

    async def fakeRole(roleName: str):
        async def _role(session: AsyncSession, objectType: str, ent) -> CompileRoleResult:
            calls.append(f"{roleName}:{objectType}:{ent.id}")
            return CompileRoleResult(role=roleName, status="success", detail="ok")

        return _role

    svc._compileGraph = await fakeRole("graph")
    svc._compileVector = await fakeRole("vector")
    svc._compileSqlMetadata = await fakeRole("sql_metadata")

    result = await svc.compileObject(dbSession, f"obj:class:{entity.id}")

    assert isinstance(result, CompileObjectResult)
    assert calls == [
        f"graph:class:{entity.id}",
        f"vector:class:{entity.id}",
        f"sql_metadata:class:{entity.id}",
    ]
    assert result.unified_id == f"obj:class:{entity.id}"
    assert result.object_type == "class"
    assert result.ontology_id == entity.id
    assert result.isFullySuccessful is True
    assert [r.status for r in result.roles] == ["success", "success", "success"]


async def test_compile_object_failure_isolation(dbSession: AsyncSession) -> None:
    """单角色失败不拖垮其他角色（multistep 失败隔离同款）。"""
    entity = await _seedClass(dbSession)
    svc = KnowledgeCompilerService()

    async def okRole(session: AsyncSession, objectType: str, ent) -> CompileRoleResult:
        return CompileRoleResult(role="graph", status="success")

    async def okSqlRole(session: AsyncSession, objectType: str, ent) -> CompileRoleResult:
        return CompileRoleResult(role="sql_metadata", status="success")

    async def boomRole(session: AsyncSession, objectType: str, ent) -> CompileRoleResult:
        raise RuntimeError("milvus down")

    svc._compileGraph = okRole
    svc._compileVector = boomRole
    svc._compileSqlMetadata = okSqlRole

    result = await svc.compileObject(dbSession, f"obj:class:{entity.id}")

    assert result.isFullySuccessful is False
    byRole = {r.role: r for r in result.roles}
    assert byRole["graph"].status == "success"
    assert byRole["vector"].status == "failed"
    assert "milvus down" in (byRole["vector"].error or "")
    assert byRole["sql_metadata"].status == "success"


async def test_compile_vector_milvus_outage_marks_failed_no_pg_half_product(
    dbSession: AsyncSession,
) -> None:
    """人为制造 Milvus 断连 → vector 角色标记 failed，PG 无半成品（本体行原样）。"""
    entity = await _seedClass(dbSession)
    beforeName = entity.class_name

    fakeOntology = MagicMock()
    # syncClassEmbedding 内部最终走 syncEmbedding；Milvus 断连时它抛 OntologyError。
    # 这里直接让入口抛错 = 断连等价模拟（fake，不真连）。
    fakeOntology.syncClassEmbedding = AsyncMock(
        side_effect=RuntimeError("Milvus connect failed")
    )
    svc = KnowledgeCompilerService(ontology=fakeOntology)

    # graph 角色 fake 成功，隔离变量只剩 vector
    async def okGraph(session: AsyncSession, objectType: str, ent) -> CompileRoleResult:
        return CompileRoleResult(role="graph", status="success")

    svc._compileGraph = okGraph

    entityId = entity.id  # rollback 会 expire ORM 对象，先取标量
    result = await svc.compileObject(dbSession, f"obj:class:{entity.id}")

    byRole = {r.role: r for r in result.roles}
    assert byRole["vector"].status == "failed"
    assert "Milvus connect failed" in (byRole["vector"].error or "")
    # PG 无半成品：本体行未被改动（rollback 后用标量查询避免惰性加载）
    await dbSession.rollback()
    row = (
        await dbSession.execute(
            select(OntologyClass.class_name, OntologyClass.source_table).where(
                OntologyClass.id == entityId
            )
        )
    ).one()
    assert row.class_name == beforeName
    assert row.source_table is None


async def test_compile_object_unknown_id_raises_not_found(dbSession: AsyncSession) -> None:
    svc = KnowledgeCompilerService()
    with pytest.raises(NotFoundError):
        await svc.compileObject(dbSession, "obj:class:424242")


# ---------------------------------------------------------------------------
# 真实角色方法（外部库 fake，走真 PG + id_mapping）
# ---------------------------------------------------------------------------


async def test_graph_role_upserts_class_node_and_registers_mapping(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    entity = await _seedClass(dbSession)
    captured: dict = {}

    def fakeUpsertClassNode(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(kc_module.neo4j, "upsertClassNode", fakeUpsertClassNode)

    svc = KnowledgeCompilerService()
    role = await svc._compileGraph(dbSession, "class", entity)

    assert role.status == "success"
    assert captured["name"] == "供应商"
    # unified_id 与 id_mapping 表登记一致
    mapping = (
        await dbSession.execute(
            select(IdMapping).where(
                IdMapping.business_object == "CLASS",
                IdMapping.external_id == str(entity.id),
            )
        )
    ).scalar_one()
    assert captured["unified_id"] == mapping.unified_id


async def test_vector_role_property_uses_existing_sync_entry(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """property 走薄包装：generateEmbedding（fake）+ syncEmbedding（fake 入口）。"""
    cls = OntologyClass(class_name="订单")
    dbSession.add(cls)
    await dbSession.commit()
    await dbSession.refresh(cls)
    prop = OntologyProperty(
        class_id=cls.id, property_name="order_no", data_type="STRING"
    )
    dbSession.add(prop)
    await dbSession.commit()
    await dbSession.refresh(prop)

    async def fakeGenerateEmbedding(self, text: str) -> list[float]:
        return [0.1] * 1024

    monkeypatch.setattr(
        kc_module.EmbeddingService, "generateEmbedding", fakeGenerateEmbedding
    )
    fakeOntology = MagicMock()
    fakeOntology.syncEmbedding = MagicMock()

    svc = KnowledgeCompilerService(ontology=fakeOntology)
    role = await svc._compileVector(dbSession, "property", prop)

    assert role.status == "success"
    fakeOntology.syncEmbedding.assert_called_once()
    kwargs = fakeOntology.syncEmbedding.call_args.kwargs
    assert kwargs["ontologyId"] == prop.id
    assert kwargs["type"] == "property"
    assert len(kwargs["embedding"]) == 1024


async def test_sql_metadata_role_skips_non_metric(dbSession: AsyncSession) -> None:
    entity = await _seedClass(dbSession)
    svc = KnowledgeCompilerService()
    role = await svc._compileSqlMetadata(dbSession, "class", entity)
    assert role.status == "skipped"


async def test_sql_metadata_role_refreshes_linked_kpis(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    metric = OntologyMetric(
        metric_name="采购金额", formula="SUM(amount)", agg_function="SUM"
    )
    dbSession.add(metric)
    await dbSession.commit()
    await dbSession.refresh(metric)
    kpi = KpiCatalog(
        kpi_code="KPI_TEST_LINKED", kpi_name="采购金额", formula="SUM(amount)",
        status="DRAFT", metric_id=metric.id,
    )
    dbSession.add(kpi)
    await dbSession.commit()

    refreshed: list[str] = []

    async def fakeRefreshOne(session: AsyncSession, kpi_code: str) -> None:
        refreshed.append(kpi_code)

    monkeypatch.setattr(
        get_kpi_match_cache(), "refreshOne", fakeRefreshOne
    )

    svc = KnowledgeCompilerService()
    role = await svc._compileSqlMetadata(dbSession, "metric", metric)

    assert role.status == "success"
    assert refreshed == ["KPI_TEST_LINKED"]
    assert "KPI_TEST_LINKED" in (role.detail or "")


async def test_sql_metadata_role_metric_without_kpi_is_skipped(
    dbSession: AsyncSession,
) -> None:
    metric = OntologyMetric(
        metric_name="无挂靠指标", formula="SUM(x)", agg_function="SUM"
    )
    dbSession.add(metric)
    await dbSession.commit()
    await dbSession.refresh(metric)

    svc = KnowledgeCompilerService()
    role = await svc._compileSqlMetadata(dbSession, "metric", metric)
    assert role.status == "skipped"


# ---------------------------------------------------------------------------
# reconcile：聚合 + warning + audit 落库
# ---------------------------------------------------------------------------


def _fakeStoreCounts(monkeypatch: pytest.MonkeyPatch, pg: dict, neo4j: dict, milvus: dict) -> None:
    async def fakeCountPg(session: AsyncSession) -> dict:
        return dict(pg)

    def fakeCountNeo4j() -> dict:
        return dict(neo4j)

    def fakeCountMilvus() -> dict:
        return dict(milvus)

    monkeypatch.setattr(kc_module.KnowledgeCompilerService, "_countPg", staticmethod(fakeCountPg))
    monkeypatch.setattr(kc_module.KnowledgeCompilerService, "_countNeo4j", staticmethod(fakeCountNeo4j))
    monkeypatch.setattr(kc_module.KnowledgeCompilerService, "_countMilvus", staticmethod(fakeCountMilvus))


async def test_reconcile_consistent_empty_stores_writes_audit(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakeStoreCounts(monkeypatch, {"class": 0, "property": 0, "metric": 0},
                     {"class": 0, "property": 0, "metric": 0},
                     {"class": 0, "property": 0, "metric": 0})
    svc = KnowledgeCompilerService()
    summary = await svc.reconcile(dbSession)

    assert isinstance(summary, ReconcileSummary)
    assert summary.isConsistent is True
    assert summary.warnings == ()
    row = (
        await dbSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "knowledge_compile_reconcile"
            )
        )
    ).scalars().all()
    assert len(row) == 1, "reconcile 结果必须落 audit_history（audit_log）"
    assert row[0].action == "CREATE"


async def test_reconcile_count_mismatch_produces_warning(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fakeStoreCounts(monkeypatch, {"class": 3, "property": 0, "metric": 0},
                     {"class": 2, "property": 0, "metric": 0},
                     {"class": 3, "property": 0, "metric": 0})
    svc = KnowledgeCompilerService()
    summary = await svc.reconcile(dbSession)

    assert summary.isConsistent is False
    assert any("class" in w for w in summary.warnings)
    payload = summary.toDict()
    assert payload["pg_counts"]["class"] == 3
    assert payload["neo4j_counts"]["class"] == 2


async def test_reconcile_store_unreachable_marks_inconsistent(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def brokenPg(session: AsyncSession) -> dict:
        raise RuntimeError("pg unreachable")

    def fakeNeo4j() -> dict:
        return {"class": 0, "property": 0, "metric": 0}

    def fakeMilvus() -> dict:
        return {"class": 0, "property": 0, "metric": 0}

    monkeypatch.setattr(kc_module.KnowledgeCompilerService, "_countPg", staticmethod(brokenPg))
    monkeypatch.setattr(kc_module.KnowledgeCompilerService, "_countNeo4j", staticmethod(fakeNeo4j))
    monkeypatch.setattr(kc_module.KnowledgeCompilerService, "_countMilvus", staticmethod(fakeMilvus))

    svc = KnowledgeCompilerService()
    summary = await svc.reconcile(dbSession)

    assert summary.isConsistent is False
    assert any("pg" in w.lower() for w in summary.warnings)
    assert summary.pg_counts is None


# ---------------------------------------------------------------------------
# 调度纯函数（agent_scheduler 模式：croniter）
# ---------------------------------------------------------------------------


def test_next_reconcile_run_daily_cron() -> None:
    nextRun = KnowledgeCompilerService.nextReconcileRun(_NOW)
    assert nextRun > _NOW
    assert nextRun <= _NOW + timedelta(days=1)


def test_is_reconcile_due() -> None:
    # 从未跑过 → 立即到期
    assert KnowledgeCompilerService.isReconcileDue(None, _NOW) is True
    # 上次跑完还没到下一次 cron → 未到期
    recent = _NOW - timedelta(hours=1)
    assert KnowledgeCompilerService.isReconcileDue(recent, _NOW) is False
    # 上次跑完已过下一个 cron 触发点 → 到期
    stale = _NOW - timedelta(days=2)
    assert KnowledgeCompilerService.isReconcileDue(stale, _NOW) is True
