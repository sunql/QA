"""候选/问题业务决断（G5 前台签收写口）：真实 PG。

1. 合法决断落 `status/decided_by/decided_time/decision_note`；
2. 非法取值 ⇒ 422 语义；未知 id ⇒ 404 语义；
3. **允许改判**（待证 → 采纳），只保留最近一次；
4. 问题状态四态 + 备注。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryFinding,
    DiscoveryRun,
)
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataSource
from app.infrastructure.security.crypto import encryptApiKey
from app.services.discovery_candidate_review import DiscoveryCandidateReviewService


async def _seed(session: Any) -> tuple[int, int]:
    datasource = DataSource(
        name="REVIEW_SRC",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
    )
    session.add(datasource)
    await session.flush()
    source = DiscoverySource(
        name="评审源",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
    )
    session.add(source)
    await session.flush()
    snapshot = MetadataSnapshot(
        source_id=source.id,
        schema_hash="1" * 64,
        snapshot_json={"schemaVersion": "v2", "tables": []},
        table_count=0,
        column_count=0,
    )
    session.add(snapshot)
    await session.flush()
    run = DiscoveryRun(source_id=source.id, snapshot_id=snapshot.id, status="SUCCEEDED")
    session.add(run)
    await session.flush()
    candidate = DiscoveryCandidate(
        run_id=run.id,
        source_id=source.id,
        snapshot_id=snapshot.id,
        candidate_type="OBJECT",
        table_name="DIM_SUPPLIER",
        payload={"className": "Supplier"},
        evidence_refs=[{"table": "DIM_SUPPLIER", "column": "BPSNUM_0"}],
        status="CANDIDATE",
    )
    finding = DiscoveryFinding(
        run_id=run.id,
        source_id=source.id,
        snapshot_id=snapshot.id,
        finding_type="MISSING",
        severity="WARNING",
        detail="测试问题",
    )
    session.add_all([candidate, finding])
    await session.commit()
    await session.refresh(candidate)
    await session.refresh(finding)
    return candidate.id, finding.id


async def test_decide_candidate_records_actor_time_and_note(dbSession: Any) -> None:
    candidateId, _ = await _seed(dbSession)

    row = await DiscoveryCandidateReviewService().decideCandidate(
        dbSession,
        candidateId=candidateId,
        status="rejected",
        note="语义不符：该列是付款条件",
        actor="biz-reviewer",
    )

    assert row.status == "REJECTED"
    assert row.decided_by == "biz-reviewer"
    assert row.decided_time is not None
    assert row.decision_note == "语义不符：该列是付款条件"


async def test_candidate_can_be_redecided(dbSession: Any) -> None:
    candidateId, _ = await _seed(dbSession)
    service = DiscoveryCandidateReviewService()
    await service.decideCandidate(
        dbSession, candidateId=candidateId, status="NEEDS_EVIDENCE", actor="biz"
    )

    row = await service.decideCandidate(
        dbSession, candidateId=candidateId, status="ACCEPTED",
        note="补证完成", actor="biz",
    )

    assert row.status == "ACCEPTED"
    assert row.decision_note == "补证完成"


async def test_invalid_candidate_status_is_rejected(dbSession: Any) -> None:
    candidateId, _ = await _seed(dbSession)

    with pytest.raises(ValidationError):
        await DiscoveryCandidateReviewService().decideCandidate(
            dbSession, candidateId=candidateId, status="MAYBE", actor="biz"
        )


async def test_unknown_candidate_is_not_found(dbSession: Any) -> None:
    with pytest.raises(NotFoundError):
        await DiscoveryCandidateReviewService().decideCandidate(
            dbSession, candidateId=999999, status="ACCEPTED", actor="biz"
        )


async def test_resolve_finding_updates_status_and_note(dbSession: Any) -> None:
    _, findingId = await _seed(dbSession)

    row = await DiscoveryCandidateReviewService().resolveFinding(
        dbSession, findingId=findingId, status="acknowledged", note="已指派张三", actor="biz"
    )

    assert row.status == "ACKNOWLEDGED"
    assert row.decision_note == "已指派张三"


async def test_invalid_finding_status_is_rejected(dbSession: Any) -> None:
    _, findingId = await _seed(dbSession)

    with pytest.raises(ValidationError):
        await DiscoveryCandidateReviewService().resolveFinding(
            dbSession, findingId=findingId, status="DONE", actor="biz"
        )
