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
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import AuditLog
from app.domain.semantic_manifest_schema import manifestHash
from app.domain.semantic_release_models import SemanticRelease
from app.services import semantic_release_service as releaseServiceModule
from app.services.semantic_registry_service import (
    SemanticReleaseIntegrityError,
    semanticRegistry,
)
from app.services.semantic_release_service import semanticReleaseService

_MINIMAL_MANIFEST: dict[str, Any] = {
    "schemaVersion": "v1",
    "domains": [],
    "classes": [],
    "joins": [],
    "metrics": [],
    "kpis": [],
    "businessObjects": [],
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
    assert row.schema_version == "v1"
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
