"""语义发布写路径（P1b / D10）的真实 PG 全链路。

与 `unit/test_semantic_release_service.py` 分工：那边钉纯逻辑（闸门分级、迁移矩阵、
撞库翻译），这边钉**数据库真实行为**：

1. 编译 → 落库 DRAFT：哈希 / schemaVersion 与真实治理表一致；
2. 完整生命周期 DRAFT → VALIDATED → ACTIVE → ARCHIVED，每一步都有审计行；
3. 激活是「顶替」语义：同域原 ACTIVE 在同一事务里归档；全局与按域互不影响；
4. 非法迁移 409、闸门阻断 409、坏哈希 fail-closed；
5. 写时失效：激活后 `getActive` 拿到新快照，归档后回到 None。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import (
    AgentDefinition,
    AgentToolConfig,
    AuditLog,
    BusinessObject,
    DataDomain,
    DataSource,
    IdMapping,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
    OntologyRelation,
)
from app.domain.procurement_binding_models import ProcurementSourceBinding
from app.domain.semantic_manifest_schema import SCHEMA_VERSION, manifestHash
from app.domain.semantic_release_check_models import SemanticReleaseCheck
from app.domain.semantic_release_models import SemanticRelease
from app.services import semantic_release_service as releaseServiceModule
from app.services.semantic_graph_mirror_resolution import resolveManifestEntities
from app.services.semantic_registry_service import (
    SemanticReleaseIntegrityError,
    semanticRegistry,
)
from app.services.semantic_release_checks import (
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    PublishCheck,
)
from app.services.semantic_release_context_facts import (
    loadActiveDatasourceNames,
    loadAgentViolations,
    loadRelationViolations,
)
from app.services.semantic_release_service import semanticReleaseService
from app.tests._knowledge_manifest import sampleManifest

_MINIMAL_MANIFEST: dict[str, Any] = {
    "schemaVersion": SCHEMA_VERSION,
    "domains": [],
    "classes": [],
    "joins": [],
    "metrics": [],
    "kpis": [],
    "businessObjects": [],
    "sourceBindings": [],
}

# 一条「已发布但无落点」的 RATIO 指标：Schema 合法，发布闸门必须阻断。
_BLOCKED_MANIFEST: dict[str, Any] = {
    **_MINIMAL_MANIFEST,
    "metrics": [
        {
            "metricName": "M_NO_LANDING",
            "metricAlias": None,
            "formula": "a / b",
            "aggFunction": "SUM",
            "metricType": "RATIO",
            "targetClassName": None,
            "dimensionDefaults": None,
            "grainKeys": [],
            "aggregationOrder": None,
            "numeratorMeasure": {
                "agg": "SUM",
                "expr": None,
                "filter": None,
                "className": None,
                "classAlias": None,
            },
            "denominatorMeasure": None,
            "filterExpression": None,
            "status": "PUBLISHED",
            "owner": None,
            "executionSource": "NONE",
            "sources": [],
            "kpiCodes": [],
        }
    ],
}


@pytest.fixture()
async def session(dbSession: AsyncSession) -> AsyncIterator[AsyncSession]:
    """直接复用集成测试的真实 PG 会话（每测试 TRUNCATE）。"""
    yield dbSession


async def _insertRelease(
    session: AsyncSession,
    *,
    version: str,
    status: str = "DRAFT",
    domainCode: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> SemanticRelease:
    body = manifest if manifest is not None else dict(_MINIMAL_MANIFEST)
    row = SemanticRelease(
        version=version,
        status=status,
        domain_code=domainCode,
        manifest_json=body,
        manifest_hash=manifestHash(body),
        # 与生产写路径同一口径：行版本跟着正文走（不替正文盖当前版本）。
        schema_version=body.get("schemaVersion") or SCHEMA_VERSION,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _auditActions(session: AsyncSession, releaseId: int) -> list[str]:
    rows = (
        await session.execute(
            select(AuditLog.action)
            .where(
                AuditLog.entity_type == "semantic_release",
                AuditLog.entity_id == releaseId,
            )
            .order_by(AuditLog.id)
        )
    ).scalars()
    return list(rows)


# ---------------------------------------------------------------------------
# 创建 DRAFT
# ---------------------------------------------------------------------------


async def test_create_draft_compiles_the_current_governance_tables(
    session: AsyncSession,
) -> None:
    operation = await semanticReleaseService.createDraft(
        session, version="2026.11.10-it-1", actor="tester"
    )
    row = operation.release

    assert row.id > 0
    assert row.status == "DRAFT"
    assert row.domain_code is None
    assert row.created_by == "tester"
    # 落库的哈希必须能由落库内容复算（评审要能独立验证）。
    assert row.manifest_hash == manifestHash(row.manifest_json)
    # 落库行的 schema 版本 = 编译产物的版本（当前代码的 SCHEMA_VERSION）。
    assert row.schema_version == SCHEMA_VERSION
    assert row.manifest_json["schemaVersion"] == SCHEMA_VERSION
    assert isinstance(operation.blockers, tuple)
    assert await _auditActions(session, row.id) == ["CREATE"]


async def test_duplicate_version_is_a_conflict(session: AsyncSession) -> None:
    await semanticReleaseService.createDraft(session, version="2026.11.10-dup", actor="tester")

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-dup", actor="tester"
        )

    assert "2026.11.10-dup" in str(caught.value)


async def test_unknown_domain_is_rejected_before_compiling(session: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-dom", actor="tester", domainCode="NO_SUCH_DOMAIN"
        )


async def test_blank_version_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await semanticReleaseService.createDraft(session, version="   ", actor="tester")


async def test_oversized_actor_is_rejected_before_compiling(session: AsyncSession) -> None:
    """身份字段超列宽若交给 asyncpg 会变成 DataError 500；必须在编译前 422。"""
    with pytest.raises(ValidationError):
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-actor", actor="x" * 65
        )


async def test_oversized_departments_are_rejected(session: AsyncSession) -> None:
    """审计列 actor_departments 是 VARCHAR(500)：不截断，直接拒绝。"""
    with pytest.raises(ValidationError):
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.10-dept",
            actor="tester",
            departments=("采购部" * 200,),
        )


async def test_oversized_manifest_is_rejected_before_insert(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写路径比读路径更早拒绝：异常快照根本不该落库。"""
    monkeypatch.setattr(releaseServiceModule, "MAX_MANIFEST_BYTES", 16)

    with pytest.raises(ValidationError) as caught:
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-big", actor="tester"
        )

    assert "字节" in str(caught.value)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_full_lifecycle_sets_status_time_and_audit(session: AsyncSession) -> None:
    draft = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-life", actor="tester"
        )
    ).release

    # 先焐热缓存（空结果也会被缓存，见 D8）：激活后若不失效，这里会一直读到 None。
    assert await semanticRegistry.getActive(session) is None

    validated = await semanticReleaseService.markValidated(
        session, draft.id, actor="tester"
    )
    assert validated.release.status == "VALIDATED"

    activated = await semanticReleaseService.activate(session, draft.id, actor="tester")
    assert activated.release.status == "ACTIVE"
    assert activated.release.activated_time is not None

    active = await semanticRegistry.getActive(session)
    assert active is not None and active.id == draft.id

    await semanticReleaseService.archive(session, draft.id, actor="tester")
    await session.refresh(draft)
    assert draft.status == "ARCHIVED"
    assert await semanticRegistry.getActive(session) is None

    assert await _auditActions(session, draft.id) == [
        "CREATE",
        "UPDATE",
        "UPDATE",
        "UPDATE",
    ]


async def test_activation_archives_the_previous_active_in_the_same_scope(
    session: AsyncSession,
) -> None:
    first = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-first", actor="tester"
        )
    ).release
    second = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-second", actor="tester"
        )
    ).release

    await semanticReleaseService.markValidated(session, first.id, actor="tester")
    await semanticReleaseService.activate(session, first.id, actor="tester")
    await semanticReleaseService.markValidated(session, second.id, actor="tester")
    await semanticReleaseService.activate(session, second.id, actor="tester")

    await session.refresh(first)
    assert first.status == "ARCHIVED"
    active = await semanticRegistry.getActive(session)
    assert active is not None and active.id == second.id
    # 旧版被归档也要留审计（ACTIVE→ARCHIVED 是独立一次迁移）。
    assert (await _auditActions(session, first.id))[-1] == "UPDATE"


async def test_activation_can_roll_back_to_an_older_release(
    session: AsyncSession,
) -> None:
    """回滚方向：目标 id 小于现有 ACTIVE。

    SQLAlchemy 按主键排序 flush；若不在归档后显式 flush，较新的 ACTIVE 会先于
    较旧的归档落库，瞬时两条 ACTIVE 撞 `uq_semantic_release_one_active`，
    合法的「回滚到历史版本」会确定性失败并被误报成并发冲突。
    """
    older = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-older", actor="tester"
        )
    ).release
    newer = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-newer", actor="tester"
        )
    ).release
    assert older.id < newer.id

    await semanticReleaseService.markValidated(session, newer.id, actor="tester")
    await semanticReleaseService.activate(session, newer.id, actor="tester")

    await semanticReleaseService.markValidated(session, older.id, actor="tester")
    await semanticReleaseService.activate(session, older.id, actor="tester")

    await session.refresh(newer)
    assert newer.status == "ARCHIVED"
    active = await semanticRegistry.getActive(session)
    assert active is not None and active.id == older.id


async def test_domain_activation_does_not_touch_the_global_snapshot(
    session: AsyncSession,
) -> None:
    globalDraft = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-global", actor="tester"
        )
    ).release
    domainDraft = (
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.10-domain",
            actor="tester",
            domainCode="PROCUREMENT",
        )
    ).release

    for draft in (globalDraft, domainDraft):
        await semanticReleaseService.markValidated(session, draft.id, actor="tester")
        await semanticReleaseService.activate(session, draft.id, actor="tester")

    await session.refresh(globalDraft)
    assert globalDraft.status == "ACTIVE"

    globalActive = await semanticRegistry.getActive(session)
    domainActive = await semanticRegistry.getActive(session, domainCode="PROCUREMENT")
    assert globalActive is not None and globalActive.id == globalDraft.id
    assert domainActive is not None and domainActive.id == domainDraft.id


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


async def test_illegal_transitions_are_conflicts(session: AsyncSession) -> None:
    draft = await _insertRelease(session, version="2026.11.10-draft", status="DRAFT")
    validated = await _insertRelease(session, version="2026.11.10-val", status="VALIDATED")
    active = await _insertRelease(session, version="2026.11.10-act", status="ACTIVE")
    archived = await _insertRelease(session, version="2026.11.10-arc", status="ARCHIVED")

    cases = [
        (semanticReleaseService.activate, draft.id),
        (semanticReleaseService.markValidated, validated.id),
        (semanticReleaseService.archive, validated.id),
        (semanticReleaseService.markValidated, archived.id),
        (semanticReleaseService.activate, archived.id),
        (semanticReleaseService.archive, archived.id),
    ]
    for action, releaseId in cases:
        with pytest.raises(ConflictError):
            await action(session, releaseId, actor="tester")

    # ACTIVE 行本身没被上面任何一次失败调用改坏。
    await session.refresh(active)
    assert active.status == "ACTIVE"


async def test_publish_gate_blocks_validation(session: AsyncSession) -> None:
    row = await _insertRelease(
        session, version="2026.11.10-blocked", manifest=dict(_BLOCKED_MANIFEST)
    )

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.markValidated(session, row.id, actor="tester")

    assert "发布闸门" in str(caught.value)
    await session.refresh(row)
    assert row.status == "DRAFT"


async def test_tampered_snapshot_fails_closed_on_validate(session: AsyncSession) -> None:
    """绕过写路径改坏哈希：校验在状态迁移之前，整条操作失败。"""
    row = await _insertRelease(session, version="2026.11.10-bad", status="DRAFT")
    row.manifest_hash = "b" * 64
    await session.commit()

    with pytest.raises(SemanticReleaseIntegrityError):
        await semanticReleaseService.markValidated(session, row.id, actor="tester")


async def test_missing_release_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await semanticReleaseService.markValidated(session, 999_999, actor="tester")


async def test_audit_rows_carry_before_and_after(
    session: AsyncSession,
) -> None:
    """审计是治理动作的唯一留痕：before/after 必须都在。"""
    draft = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.10-audit", actor="tester", departments=("采购部",)
        )
    ).release
    await semanticReleaseService.markValidated(session, draft.id, actor="tester")

    rows = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "semantic_release",
                AuditLog.entity_id == draft.id,
            )
            .order_by(AuditLog.id)
        )
    ).scalars().all()

    assert [row.action for row in rows] == ["CREATE", "UPDATE"]
    assert rows[0].before_json is None
    assert rows[0].after_json == {
        "status": "DRAFT",
        "version": "2026.11.10-audit",
        "domainCode": None,
    }
    assert rows[1].before_json == {
        "status": "DRAFT",
        "version": "2026.11.10-audit",
        "domainCode": None,
    }
    assert rows[1].after_json["status"] == "VALIDATED"
    assert rows[0].actor == "tester"
    assert rows[0].actor_departments == "采购部"


async def test_only_one_active_row_survives_concurrent_scope(
    session: AsyncSession,
) -> None:
    """库侧部分唯一索引的兜底：手工把两行都改成 ACTIVE 必被拒绝。"""
    first = await _insertRelease(session, version="2026.11.10-a1", status="ACTIVE")
    second = await _insertRelease(session, version="2026.11.10-a2", status="VALIDATED")

    second.status = "ACTIVE"
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    activeCount = await session.scalar(
        select(func.count())
        .select_from(SemanticRelease)
        .where(SemanticRelease.status == "ACTIVE")
    )
    assert activeCount == 1
    await session.refresh(first)
    assert first.status == "ACTIVE"


# =============================================================================
# P3b D22：发布镜像的名字 → unified_id 解析（真实 PG）
# =============================================================================


async def test_resolve_manifest_entities_maps_names_to_unified_ids(
    session: AsyncSession,
) -> None:
    """manifest 用名字、图谱用 unified_id：解析必须对上真实 id_mapping 行。"""
    cls = OntologyClass(
        class_name="MIRROR_CLASS_A",
        class_alias=None,
        description=None,
        source_table=None,
        object_type=None,
        object_owner=None,
        parent_class_id=None,
        created_by=None,
        version=1,
    )
    session.add(cls)
    await session.flush()
    prop = OntologyProperty(
        class_id=cls.id,
        property_name="MIRROR_PROP_A",
        property_alias=None,
        description=None,
        data_type="STRING",
        is_primary_key=False,
        is_foreign_key=False,
        source_column=None,
    )
    metric = OntologyMetric(
        metric_name="MIRROR_METRIC_A",
        metric_alias=None,
        formula="SUM(1)",
        agg_function="SUM",
        target_class_id=None,
        created_by=None,
    )
    kpi = KpiCatalog(kpi_code="MIRROR_KPI_A", kpi_name="镜像 KPI")
    session.add_all([prop, metric, kpi])
    await session.flush()
    session.add_all(
        [
            IdMapping(
                unified_id=f"obj:CLASS:{cls.id}",
                business_object="CLASS",
                external_id=str(cls.id),
                pg_table="ontology_class",
                pg_id=str(cls.id),
            ),
            IdMapping(
                unified_id=f"obj:PROPERTY:{prop.id}",
                business_object="PROPERTY",
                external_id=str(prop.id),
                pg_table="ontology_property",
                pg_id=str(prop.id),
            ),
            IdMapping(
                unified_id=f"obj:METRIC:{metric.id}",
                business_object="METRIC",
                external_id=str(metric.id),
                pg_table="ontology_metric",
                pg_id=str(metric.id),
            ),
            IdMapping(
                unified_id=f"obj:KPI:{kpi.id}",
                business_object="KPI",
                external_id=str(kpi.id),
                pg_table="kpi_catalog",
                pg_id=str(kpi.id),
            ),
        ]
    )
    await session.commit()

    resolution = await resolveManifestEntities(
        session,
        {
            "classes": [
                {
                    "className": "MIRROR_CLASS_A",
                    "properties": [{"propertyName": "MIRROR_PROP_A"}],
                }
            ],
            "metrics": [{"metricName": "MIRROR_METRIC_A"}],
            "kpis": [{"kpiCode": "MIRROR_KPI_A"}],
            "joins": [],
        },
    )

    assert resolution.classIds["MIRROR_CLASS_A"] == f"obj:CLASS:{cls.id}"
    assert resolution.propertyIds[("MIRROR_CLASS_A", "MIRROR_PROP_A")] == (
        f"obj:PROPERTY:{prop.id}"
    )
    assert resolution.metricIds["MIRROR_METRIC_A"] == f"obj:METRIC:{metric.id}"
    assert resolution.kpiIds["MIRROR_KPI_A"] == f"obj:KPI:{kpi.id}"


async def test_resolve_manifest_entities_reports_missing_entities(
    session: AsyncSession,
) -> None:
    """本体里没有的实体 ⇒ 解析不到（键不存在），镜像计划里逐条记为未解析。"""
    resolution = await resolveManifestEntities(
        session,
        {
            "classes": [{"className": "MIRROR_CLASS_MISSING", "properties": []}],
            "metrics": [],
            "kpis": [],
            "joins": [],
        },
    )
    assert "MIRROR_CLASS_MISSING" not in resolution.classIds


async def test_replace_checks_truncates_beyond_the_cap(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纵深防御：绕过契约的 manifest 也不得把一次写入撑到不可控。

    但截断**不是静默的** ⇒ 必须落一条 `CHECKS_TRUNCATED`（ERROR/FAIL）合成行：
    表里查得到「这份报告被截断」，且闸门把它当阻断项（fail-closed）。
    静默截断 = 假绿：读者看到「有结论」却不知道结论不完整。

    ⚠️ monkeypatch 打在 **checks 模块**（`capChecks` 在那里读常量），
    不是 check_service 模块 —— 打在错模块上会得到「没截断」的假绿。
    """
    from app.services import semantic_release_check_service as checkServiceModule
    from app.services import semantic_release_checks as checksModule

    release = await _insertRelease(session, version="2026.11.18-cap")
    monkeypatch.setattr(checksModule, "MAX_CHECKS_PER_RELEASE", 3)

    checks = tuple(
        PublishCheck(
            code=f"CODE_{index}",
            item=f"校验项 {index}",
            severity=SEVERITY_ERROR,
            result=RESULT_PASS,
            targetType="METRIC",
            targetKey=f"M_{index}",
            detail=f"第 {index} 项",
        )
        for index in range(5)
    )
    written = await checkServiceModule.semanticReleaseCheckService.replaceChecks(
        session, release.id, checks, actor="tester"
    )
    await session.commit()

    # 上限 3 ⇒ 保留前 2 条 + 1 条截断合成行 = 3 行。
    assert written == 3
    stored = await checkServiceModule.semanticReleaseCheckService.listChecks(
        session, release.id
    )
    assert [row.check_code for row in stored] == [
        "CODE_0",
        "CODE_1",
        "CHECKS_TRUNCATED",
    ]
    truncated = stored[-1]
    assert truncated.severity == checksModule.SEVERITY_ERROR
    assert truncated.result == checksModule.RESULT_FAIL
    assert all(row.checked_by == "tester" for row in stored)

    # 截断行必须**阻断**闸门（报告不完整 ⇒ 不得据此放行）。
    gate = checksModule.checksToGateResult(stored and tuple(
        checksModule.PublishCheck(
            code=row.check_code,
            item=row.check_item,
            severity=row.severity,
            result=row.result,
            targetType=row.target_type,
            targetKey=row.target_key,
            detail=row.detail,
        )
        for row in stored
    ))
    assert gate.canPublish is False


async def test_replace_checks_overwrites_previous_rows(session: AsyncSession) -> None:
    """覆盖写：第二次评估只保留新结论（旧行被删），行数不随评估轮次增长。"""
    from app.services import semantic_release_check_service as checkServiceModule

    release = await _insertRelease(session, version="2026.11.18-overwrite")
    service = checkServiceModule.semanticReleaseCheckService

    await service.replaceChecks(
        session,
        release.id,
        (
            PublishCheck(
                code="CODE_OLD",
                item="旧校验项",
                severity=SEVERITY_ERROR,
                result=RESULT_FAIL,
                detail="旧结论",
            ),
        ),
        actor="tester",
    )
    await service.replaceChecks(
        session,
        release.id,
        (
            PublishCheck(
                code="CODE_NEW",
                item="新校验项",
                severity=SEVERITY_ERROR,
                result=RESULT_PASS,
                detail="新结论",
            ),
        ),
        actor="tester",
    )
    await session.commit()

    stored = await service.listChecks(session, release.id)
    assert [row.check_code for row in stored] == ["CODE_NEW"]


# ---------------------------------------------------------------------------
# 失败路径留痕（证据先行）
# ---------------------------------------------------------------------------


async def test_blocked_validation_leaves_an_audit_trace(
    session: AsyncSession,
) -> None:
    """**失败尝试也留痕**（评审 HIGH）：check 表是覆盖写的。

    唯一写 audit_log 的地方是「成功」的状态迁移 ⇒ 被闸门拦下的那次尝试
    在 check 表与 audit_log 两边都没有痕迹：下一轮重评估把上一轮的 FAIL 行删掉后，
    「那次被哪一条拦下的」就再也查不到了 —— 而这正是这张表要回答的问题。

    ⇒ 失败路径必须补一条审计（action 复用封闭词表里的 `UPDATE`）。
    """
    row = await _insertRelease(
        session, version="2026.11.18-blocked-audit", manifest=dict(_BLOCKED_MANIFEST)
    )

    with pytest.raises(ConflictError):
        await semanticReleaseService.markValidated(session, row.id, actor="tester")

    auditRows = (
        (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "semantic_release",
                    AuditLog.entity_id == row.id,
                )
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )

    assert len(auditRows) == 1, "失败尝试必须留下恰好一条审计"
    audit = auditRows[0]
    assert audit.action == "UPDATE"
    after = audit.after_json or {}
    assert after["blocked"] is True
    assert after["phase"] == "validate"
    assert after["status"] == "DRAFT", "状态没变（失败不改状态）"
    assert after["blockerCount"] >= 1
    assert after["blockers"], "必须记下是被哪一条拦下的"
    assert after["errorCount"] >= 1
    assert after["checkCount"] >= 1


async def test_blocked_activation_leaves_an_audit_trace(session: AsyncSession) -> None:
    """activate 被拦 ⇒ 同样留痕，且 `phase` 能区分是哪一步被拦的。"""
    row = await _insertRelease(
        session,
        version="2026.11.18-blocked-activate",
        status="VALIDATED",
        manifest=dict(_BLOCKED_MANIFEST),
    )

    with pytest.raises(ConflictError):
        await semanticReleaseService.activate(session, row.id, actor="tester")

    auditRows = (
        (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "semantic_release",
                    AuditLog.entity_id == row.id,
                )
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )

    assert len(auditRows) == 1
    after = auditRows[0].after_json or {}
    assert after["blocked"] is True
    assert after["phase"] == "activate"
    assert after["status"] == "VALIDATED"

    await session.refresh(row)
    assert row.status == "VALIDATED", "被拦下的激活不得改状态"


async def test_successful_validation_leaves_no_blocked_audit(
    session: AsyncSession,
) -> None:
    """正常路径**不得**多写一条 blocked 审计（否则审计里全是噪音）。"""
    draft = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.18-clean-audit", actor="tester"
        )
    ).release
    await semanticReleaseService.markValidated(session, draft.id, actor="tester")

    auditRows = (
        (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "semantic_release",
                    AuditLog.entity_id == draft.id,
                )
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )

    assert [row.action for row in auditRows] == ["CREATE", "UPDATE"]
    assert all((row.after_json or {}).get("blocked") is not True for row in auditRows)


# ---------------------------------------------------------------------------
# 采购域发布编排（M1-1.3 批次 B）：域切片 / 源绑定 / 回滚
#
# 为什么并进本文件而不是新建 integration 文件：
# CI 的 integration 分片按「排序后下标 mod 4」轮转 ⇒ **新建 integration 文件
# 会移动分片归属** ⇒ 原本绿的用例落到别的机器上失败（净新增失败 > 0 ⇒ 闸门转红）。
# ⇒ 新集成用例一律并进**已存在的** integration 文件。
# ---------------------------------------------------------------------------


async def _seedDomainClass(
    session: AsyncSession,
    *,
    domainCode: str,
    className: str,
    boCode: str,
) -> int:
    """种「一个域 + 一个本体类 + 一个业务对象」，返回 classId。"""
    if await session.get(DataDomain, domainCode) is None:
        session.add(DataDomain(code=domainCode, name=domainCode, enabled=True))
    cls = OntologyClass(class_name=className, source_table=f"T_{className}")
    session.add(cls)
    await session.flush()
    session.add(
        BusinessObject(
            code=boCode,
            name=boCode,
            header_class_id=cls.id,
            primary_domain_id=domainCode,
        )
    )
    await session.commit()
    return cls.id


async def _seedDatasource(session: AsyncSession, name: str = "采购源") -> int:
    datasource = DataSource(
        name=name,
        type="oracle",
        host="db.example.internal",
        port=1521,
        database_name="ZJTH",
        username="qa_readonly",
        password_encrypted="encrypted",
        is_active=True,
    )
    session.add(datasource)
    await session.commit()
    await session.refresh(datasource)
    return datasource.id


async def _seedBinding(
    session: AsyncSession,
    *,
    classId: int,
    datasourceId: int,
    sourceTable: str,
    isCandidate: bool,
) -> None:
    """种一条源绑定（候选 or 已确认）。"""
    row = ProcurementSourceBinding(
        class_id=classId,
        datasource_id=datasourceId,
        schema_name="ZJTH",
        source_table=sourceTable,
        binding_role="BASE",
        is_candidate=isCandidate,
        confirmed_by=None if isCandidate else "tester",
        confirmed_time=None if isCandidate else datetime.now(UTC),
    )
    session.add(row)
    await session.commit()


async def _checksOf(
    session: AsyncSession, releaseId: int
) -> list[SemanticReleaseCheck]:
    return list(
        (
            await session.execute(
                select(SemanticReleaseCheck)
                .where(SemanticReleaseCheck.release_id == releaseId)
                .order_by(SemanticReleaseCheck.id)
            )
        )
        .scalars()
        .all()
    )


# --- 域切片 ----------------------------------------------------------------


async def test_domain_draft_stores_only_the_domain_slice(session: AsyncSession) -> None:
    """域发布存的是**切片**，不是全量（否则换个 domain 参数结果不变）。"""
    await _seedDomainClass(
        session, domainCode="PROCB1", className="采购订单B1", boCode="PUR_ORDER_B1"
    )
    await _seedDomainClass(
        session, domainCode="QUALB1", className="质检记录B1", boCode="QA_RECORD_B1"
    )

    draft = (
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.19-proc",
            actor="tester",
            domainCode="PROCB1",
        )
    ).release

    assert {cls["className"] for cls in draft.manifest_json["classes"]} == {"采购订单B1"}
    assert [entry["code"] for entry in draft.manifest_json["domains"]] == ["PROCB1"]
    assert [bo["code"] for bo in draft.manifest_json["businessObjects"]] == ["PUR_ORDER_B1"]


async def test_domain_release_without_anchor_is_blocked(session: AsyncSession) -> None:
    """域存在但没有业务对象挂靠 ⇒ 切片为空 ⇒ 不能发布一份空快照。"""
    session.add(DataDomain(code="PROCB2", name="采购域B2", enabled=True))
    await session.commit()

    draft = (
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.19-empty",
            actor="tester",
            domainCode="PROCB2",
        )
    ).release

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.markValidated(session, draft.id, actor="tester")

    assert "发布闸门" in str(caught.value)


# --- 源绑定 ----------------------------------------------------------------


async def test_binding_class_without_join_edge_blocks_validation(
    session: AsyncSession,
) -> None:
    """绑定类在整个本体里零 JOIN 边（孤岛）⇒ ERROR ⇒ 不得发出去。"""
    classId = await _seedDomainClass(
        session, domainCode="PROCB1", className="采购订单B1", boCode="PUR_ORDER_B1"
    )
    datasourceId = await _seedDatasource(session)
    await _seedBinding(
        session,
        classId=classId,
        datasourceId=datasourceId,
        sourceTable="DWD_PO",
        isCandidate=False,
    )

    draft = (
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.19-island",
            actor="tester",
            domainCode="PROCB1",
        )
    ).release

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.markValidated(session, draft.id, actor="tester")

    assert "发布闸门" in str(caught.value)
    joinRows = [
        row
        for row in await _checksOf(session, draft.id)
        if row.check_code == "SOURCE_BINDING_JOIN_MISSING"
    ]
    assert len(joinRows) == 1
    assert joinRows[0].result == "FAIL"
    assert joinRows[0].severity == "ERROR"


async def test_candidate_binding_is_a_warning_not_a_blocker(
    session: AsyncSession,
) -> None:
    """候选绑定是治理债：显式暴露（WARNING）但不阻断发布。"""
    classId = await _seedDomainClass(
        session, domainCode="PROCB1", className="采购订单B1", boCode="PUR_ORDER_B1"
    )
    datasourceId = await _seedDatasource(session)
    await _seedBinding(
        session,
        classId=classId,
        datasourceId=datasourceId,
        sourceTable="DWD_PO",
        isCandidate=True,
    )

    draft = (
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.19-candidate",
            actor="tester",
            domainCode="PROCB1",
        )
    ).release
    await semanticReleaseService.markValidated(session, draft.id, actor="tester")

    candidateRows = [
        row
        for row in await _checksOf(session, draft.id)
        if row.check_code == "SOURCE_BINDING_CANDIDATE"
    ]
    assert len(candidateRows) == 1
    assert candidateRows[0].result == "FAIL"
    assert candidateRows[0].severity == "WARNING"
    # 候选不进快照（D-3）。
    assert draft.manifest_json["sourceBindings"] == []


# --- 回滚（副本式，D-11）----------------------------------------------------


async def test_rollback_copies_the_snapshot_verbatim(session: AsyncSession) -> None:
    """回滚 = 回到那个内容，不是重新编译一次 ⇒ 哈希逐字相同。"""
    source = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-src", actor="tester"
        )
    ).release
    await semanticReleaseService.markValidated(session, source.id, actor="tester")
    await semanticReleaseService.activate(session, source.id, actor="tester")

    copied = (
        await semanticReleaseService.rollback(
            session, source.id, version="2026.11.19-rb-copy", actor="tester"
        )
    ).release

    assert copied.id != source.id
    assert copied.manifest_hash == source.manifest_hash
    assert copied.manifest_json == source.manifest_json
    assert copied.status == "ACTIVE"


async def test_rollback_rejects_a_draft_source(session: AsyncSession) -> None:
    """草稿从未发布过 ⇒ 不能作为回滚来源（回滚是「回到已发布的内容」）。"""
    draft = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-draft", actor="tester"
        )
    ).release

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.rollback(
            session, draft.id, version="2026.11.19-rb-draft-2", actor="tester"
        )

    assert "回滚来源" in str(caught.value)


async def test_rollback_archives_the_previous_active(session: AsyncSession) -> None:
    """回滚后：旧 ACTIVE 被归档、源保持 ARCHIVED（历史行不复活）。"""
    source = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-a", actor="tester"
        )
    ).release
    await semanticReleaseService.markValidated(session, source.id, actor="tester")
    await semanticReleaseService.activate(session, source.id, actor="tester")
    newer = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-b", actor="tester"
        )
    ).release
    await semanticReleaseService.markValidated(session, newer.id, actor="tester")
    await semanticReleaseService.activate(session, newer.id, actor="tester")

    copied = (
        await semanticReleaseService.rollback(
            session, source.id, version="2026.11.19-rb-c", actor="tester"
        )
    ).release

    assert copied.status == "ACTIVE"
    await session.refresh(newer)
    assert newer.status == "ARCHIVED"
    await session.refresh(source)
    assert source.status == "ARCHIVED"


async def test_rollback_rejects_a_duplicate_global_version(session: AsyncSession) -> None:
    """全局版本号唯一 ⇒ 回滚也不能撞（库侧唯一索引只对全局作用域生效）。"""
    source = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-dup", actor="tester"
        )
    ).release
    await semanticReleaseService.markValidated(session, source.id, actor="tester")
    await semanticReleaseService.activate(session, source.id, actor="tester")

    with pytest.raises(ConflictError):
        await semanticReleaseService.rollback(
            session, source.id, version="2026.11.19-rb-dup", actor="tester"
        )


# ---------------------------------------------------------------------------
# 批次 B 的边界（代码审查 HIGH / MEDIUM 的处置面）
#
# 为什么并进本文件而不是新建 integration 文件：CI 的 integration 分片按
# 「排序后下标 mod 4」轮转 ⇒ 新增文件会让其后所有文件的分片归属轮转一位
# ⇒ 依赖「同分片邻居副作用」的既有用例会突然红（见 `Harness/rules/测试规范.md`）。
# ---------------------------------------------------------------------------


async def test_note_longer_than_the_limit_is_rejected(session: AsyncSession) -> None:
    """备注长度闸在服务层（`note` 是 `Text` ⇒ 库里没有列宽兜底）。

    **不截断**：截断会静默改写审计里的人类可读说明 ⇒ 宁可 422 拒绝一次写。
    """
    with pytest.raises(ValidationError):
        await semanticReleaseService.createDraft(
            session,
            version="2026.11.19-note",
            actor="tester",
            note="x" * 2001,
        )


async def test_version_is_unique_inside_a_scope_but_reusable_across_domains(
    session: AsyncSession,
) -> None:
    """版本号在**作用域内**唯一（域作用域 = 同一域内）。

    - 同域同版本号 ⇒ 409（版本号要能标识「哪一次发布」）；
    - 不同域同版本号 ⇒ 允许（版本号标识的是「哪一次发布」，不是「哪套治理内容」）。
    """
    session.add(DataDomain(code="PROCV1", name="采购域V1", enabled=True))
    session.add(DataDomain(code="PROCV2", name="采购域V2", enabled=True))
    await session.commit()

    first = await semanticReleaseService.createDraft(
        session, version="2026.11.19-scope", actor="tester", domainCode="PROCV1"
    )
    other = await semanticReleaseService.createDraft(
        session, version="2026.11.19-scope", actor="tester", domainCode="PROCV2"
    )
    assert first.release.id != other.release.id

    with pytest.raises(ConflictError):
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-scope", actor="tester", domainCode="PROCV1"
        )


async def test_rollback_rejects_a_source_with_a_stale_schema_version(
    session: AsyncSession,
) -> None:
    """源快照的 manifest 版本落后 ⇒ 409（不是「先回滚上去再说」）。

    v1 没有 `sourceBindings` 分区 ⇒ 跨版本回滚会让读路径拿到一份
    **采购绑定凭空消失**的快照。
    """
    source = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-v1", actor="tester"
        )
    ).release
    await semanticReleaseService.markValidated(session, source.id, actor="tester")
    await semanticReleaseService.activate(session, source.id, actor="tester")
    source.schema_version = "v1"
    await session.commit()

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.rollback(
            session, source.id, version="2026.11.19-rb-v1-copy", actor="tester"
        )

    assert "版本" in str(caught.value)


async def test_rollback_rejects_a_source_that_is_not_a_domain_slice(
    session: AsyncSession,
) -> None:
    """域发布的源必须是**该域的有效切片** ⇒ 否则 409。

    判据是**可复算**的：对已存正文再切一次，类集合必须相同（切片幂等）。
    这里模拟「正文是更早的算法写的」（多带了一个别的域的类）。
    """
    await _seedDomainClass(
        session, domainCode="PROCR1", className="采购订单R1", boCode="PUR_ORDER_R1"
    )
    source = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-rb-slice", actor="tester", domainCode="PROCR1"
        )
    ).release
    await semanticReleaseService.markValidated(session, source.id, actor="tester")
    await semanticReleaseService.activate(session, source.id, actor="tester")
    tampered = dict(source.manifest_json)
    tampered["classes"] = [
        *tampered["classes"],
        {
            "className": "外域类R1",
            "classAlias": "外域类R1",
            "description": None,
            "sourceTable": "T_OTHER_R1",
            "objectType": "Master",
            "objectOwner": None,
            "parentClassName": None,
            "properties": [],
        },
    ]
    source.manifest_json = tampered
    await session.commit()

    with pytest.raises(ConflictError) as caught:
        await semanticReleaseService.rollback(
            session, source.id, version="2026.11.19-rb-slice-copy", actor="tester"
        )

    assert "切片" in str(caught.value)


async def test_candidate_count_is_scoped_to_the_snapshot_classes(
    session: AsyncSession,
) -> None:
    """候选统计按**快照里的类**收窄 ⇒ 报告与快照同一宇宙。

    别的域上的候选债不该算进本域的报告（否则采购域发布会显示质检域的候选数
    ⇒ 报告与快照两个宇宙）。
    """
    otherClassId = await _seedDomainClass(
        session, domainCode="PROCC1", className="质检记录C1", boCode="QA_RECORD_C1"
    )
    await _seedDomainClass(
        session, domainCode="PROCC2", className="采购订单C2", boCode="PUR_ORDER_C2"
    )
    datasourceId = await _seedDatasource(session)
    await _seedBinding(
        session,
        classId=otherClassId,
        datasourceId=datasourceId,
        sourceTable="DWD_IQC",
        isCandidate=True,
    )

    draft = (
        await semanticReleaseService.createDraft(
            session, version="2026.11.19-scope-cand", actor="tester", domainCode="PROCC2"
        )
    ).release

    candidateRows = [
        row
        for row in await _checksOf(session, draft.id)
        if row.check_code == "SOURCE_BINDING_CANDIDATE"
    ]
    assert len(candidateRows) == 1
    # 别的域的候选不在本域快照里 ⇒ 计数为 0 ⇒ 这一行是 PASS。
    assert candidateRows[0].result == "PASS"

    # validate 阶段**同一口径**（否则草稿说 0 个候选、validate 说 3 个候选
    # ⇒ 同一份快照两个阶段的报告不一致 ⇒ 证据不可复算）。
    await semanticReleaseService.markValidated(session, draft.id, actor="tester")
    validatedRows = [
        row
        for row in await _checksOf(session, draft.id)
        if row.check_code == "SOURCE_BINDING_CANDIDATE"
    ]
    assert len(validatedRows) == 1
    assert validatedRows[0].result == "PASS"


# ---------------------------------------------------------------------------
# G2：设计稿 §5.5 校验码的活库事实（真实 PG）
# ---------------------------------------------------------------------------


async def test_active_datasource_names_exclude_disabled_sources(
    session: AsyncSession,
) -> None:
    """MET-002 的「有效」判据 = 启用中；停用源不算有效绑定。"""
    await _seedDatasource(session, name="启用源")
    session.add(
        DataSource(
            name="停用源",
            type="oracle",
            host="db.example.internal",
            port=1521,
            database_name="ZJTH",
            username="qa_readonly",
            password_encrypted="encrypted",
            is_active=False,
        )
    )
    await session.commit()

    names = await loadActiveDatasourceNames(session)

    assert "启用源" in names
    assert "停用源" not in names


async def _seedRelation(
    session: AsyncSession,
    *,
    sourceName: str,
    targetName: str,
    relationType: str,
    targetTombstoned: bool = False,
) -> None:
    source = OntologyClass(class_name=sourceName, source_table=f"T_{sourceName}")
    target = OntologyClass(
        class_name=targetName,
        source_table=f"T_{targetName}",
        valid_to=datetime.now(UTC) if targetTombstoned else None,
    )
    session.add_all([source, target])
    await session.flush()
    session.add(
        OntologyRelation(
            source_class_id=source.id,
            target_class_id=target.id,
            relation_type=relationType,
        )
    )
    await session.commit()


async def test_relation_violations_flag_bad_types_and_tombstoned_endpoints(
    session: AsyncSession,
) -> None:
    """REL-001 的活库部分：类型不在白名单 / 端点类已墓碑，都要报出来。"""
    await _seedRelation(
        session, sourceName="关系源A", targetName="关系目标A", relationType="SUPPLIES"
    )
    await _seedRelation(
        session, sourceName="关系源B", targetName="关系目标B", relationType="NOT_ALLOWED"
    )
    await _seedRelation(
        session,
        sourceName="关系源C",
        targetName="关系目标C",
        relationType="SUPPLIES",
        targetTombstoned=True,
    )

    violations = await loadRelationViolations(session)

    assert any("不在代码白名单" in item for item in violations)
    assert any("墓碑" in item for item in violations)
    assert not any("关系源A-[SUPPLIES]->关系目标A" in item for item in violations)


async def test_relation_violations_are_scoped_to_the_release_classes(
    session: AsyncSession,
) -> None:
    """按发布快照里的类收窄 ⇒ 报告与快照同一宇宙（别把别的域的关系算进来）。"""
    await _seedRelation(
        session, sourceName="域内源", targetName="域内目标", relationType="NOT_ALLOWED"
    )

    unrelated = await loadRelationViolations(session, scopeClassNames=["别的类"])
    inScope = await loadRelationViolations(session, scopeClassNames=["域内源"])

    assert unrelated == ()
    assert any("域内源" in item for item in inScope)


async def test_agent_violations_flag_unknown_domains_and_missing_tools(
    session: AsyncSession,
) -> None:
    """AGT-001 的活库部分：域不在启用词表 / 未绑定工具，都要报出来。"""
    session.add(
        AgentDefinition(
            agent_code="AGENT_BAD",
            agent_name="坏 Agent",
            status="active",
            data_domains=["NO_SUCH_DOMAIN"],
            data_layers=["DIM"],
            tool_name=None,
        )
    )
    session.add(
        AgentToolConfig(
            name="g2_probe_tool",
            data_object="SUPPLIER",
            data_layers=[],
            input_schema={},
            handler_kind="BUILTIN",
            handler_ref="g2_probe_tool",
            enabled=True,
        )
    )
    session.add(
        AgentDefinition(
            agent_code="AGENT_GOOD",
            agent_name="好 Agent",
            status="active",
            data_domains=[],
            data_layers=[],
            tool_name="g2_probe_tool",
        )
    )
    await session.commit()

    violations = await loadAgentViolations(session)

    assert any("AGENT_BAD 的数据域 NO_SUCH_DOMAIN" in item for item in violations)
    assert not any("AGENT_GOOD" in item for item in violations)


async def test_design_codes_reach_the_persisted_report_from_live_facts(
    session: AsyncSession,
) -> None:
    """端到端：createDraft/validate 的落表报告里能查到 G2 的码与活库事实。

    - `MET-002`：快照里的 KPI 绑了一个**不存在**的数据源 ⇒ 逐 KPI FAIL；
    - `AGT-001`：启用中的 Agent 数据域非法 ⇒ 一条 WARNING FAIL；
    - `CLS-001` / `REL-001`：样本 manifest 干净 ⇒ PASS（同一张表里正交并存）。
    """
    manifest = sampleManifest()
    for kpi in manifest["kpis"]:
        kpi["datasourceName"] = "不存在的源"
    session.add(
        AgentDefinition(
            agent_code="AGENT_E2E",
            agent_name="端到端 Agent",
            status="active",
            data_domains=["NO_SUCH_DOMAIN"],
            data_layers=["DIM"],
            tool_name=None,
        )
    )
    await session.commit()
    row = await _insertRelease(
        session, version="2026.11.22-g2-e2e", manifest=manifest
    )

    # 样本 manifest 的 RATIO 指标缺分母落点 ⇒ 闸门仍会阻断，
    # 但**证据先行**：校验结论在抛异常前已经落表。
    with pytest.raises(ConflictError):
        await semanticReleaseService.markValidated(session, row.id, actor="tester")

    checks = await _checksOf(session, row.id)
    byCode = {check.check_code: check for check in checks}

    assert byCode["CLS-001"].result == RESULT_PASS
    assert byCode["REL-001"].result == RESULT_PASS
    assert byCode["AGT-001"].result == RESULT_FAIL
    assert byCode["AGT-001"].severity == "WARNING"
    assert "AGENT_E2E" in byCode["AGT-001"].detail

    kpiRows = [check for check in checks if check.check_code == "MET-002"]
    assert len(kpiRows) == len(manifest["kpis"])
    assert all(check.result == RESULT_FAIL for check in kpiRows)
    assert all("不存在的源" in check.detail for check in kpiRows)
