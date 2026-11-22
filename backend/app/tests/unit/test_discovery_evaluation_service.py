"""发现评测（G4-2）：可复算指标 + verdict 三态。

1. 证据能解析到本次剖析事实 ⇒ `PASS`，`evidenceCompleteRate = 1`；
2. 有候选但证据解析不了（脏数据/人工改库）⇒ `FAIL` 并列出候选 id；
3. **没有候选** ⇒ `INCONCLUSIVE`（没结论 ≠ 质量差）；
4. `recordEvaluation` 只做契约校验（verdict / metrics / datasetVersion）。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryEvaluation,
    DiscoveryFinding,
    DiscoveryProfile,
    DiscoveryRun,
)
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataSource
from app.infrastructure.security.crypto import encryptApiKey
from app.services.discovery_evaluation_service import DiscoveryEvaluationService


async def _seedRun(
    session: Any,
    *,
    evidence: dict[str, Any] | None = None,
    withCandidate: bool = True,
) -> int:
    datasource = DataSource(
        name="EVAL_SRC",
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
        name="评测源",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
    )
    session.add(source)
    await session.flush()
    snapshot = MetadataSnapshot(
        source_id=source.id,
        schema_hash="e" * 64,
        snapshot_json={"schemaVersion": "v2", "tables": []},
        table_count=1,
        column_count=1,
    )
    session.add(snapshot)
    await session.flush()
    run = DiscoveryRun(source_id=source.id, snapshot_id=snapshot.id, status="SUCCEEDED")
    session.add(run)
    await session.flush()
    session.add(
        DiscoveryProfile(
            run_id=run.id,
            source_id=source.id,
            snapshot_id=snapshot.id,
            table_name="DIM_SUPPLIER",
            column_name="BPSNUM_0",
            row_count=100,
            sql_digest="f" * 64,
        )
    )
    if withCandidate:
        session.add(
            DiscoveryCandidate(
                run_id=run.id,
                source_id=source.id,
                snapshot_id=snapshot.id,
                candidate_type="OBJECT",
                table_name="DIM_SUPPLIER",
                payload={"className": "Supplier"},
                evidence_refs=evidence
                or [{"table": "DIM_SUPPLIER", "column": "BPSNUM_0"}],
                status="CANDIDATE",
            )
        )
        session.add(
            DiscoveryFinding(
                run_id=run.id,
                source_id=source.id,
                snapshot_id=snapshot.id,
                finding_type="MISSING",
                severity="WARNING",
                detail="测试问题",
            )
        )
    await session.commit()
    return run.id


async def test_full_evidence_run_is_pass(dbSession: Any) -> None:
    runId = await _seedRun(dbSession)

    row = await DiscoveryEvaluationService().evaluateRun(
        dbSession, runId=runId, datasetVersion="pilot-2026-11", createdBy="tester"
    )

    assert row.verdict == "PASS"
    assert row.metrics_json["candidateCount"] == 1
    assert row.metrics_json["findingCount"] == 1
    assert row.metrics_json["evidenceCompleteRate"] == 1.0
    assert row.metrics_json["byType"] == {"OBJECT": 1}
    assert row.dataset_version == "pilot-2026-11"


async def test_unresolvable_evidence_is_fail(dbSession: Any) -> None:
    runId = await _seedRun(
        dbSession, evidence=[{"table": "GHOST", "column": "X"}]
    )

    row = await DiscoveryEvaluationService().evaluateRun(
        dbSession, runId=runId, datasetVersion="pilot-2026-11"
    )

    assert row.verdict == "FAIL"
    assert row.metrics_json["evidenceCompleteRate"] == 0.0
    assert row.metrics_json["unresolvableEvidenceCandidates"]


async def test_empty_run_is_inconclusive_not_fail(dbSession: Any) -> None:
    runId = await _seedRun(dbSession, withCandidate=False)

    row = await DiscoveryEvaluationService().evaluateRun(
        dbSession, runId=runId, datasetVersion="pilot-2026-11"
    )

    assert row.verdict == "INCONCLUSIVE"
    assert row.metrics_json["candidateCount"] == 0


async def test_unknown_run_is_not_found(dbSession: Any) -> None:
    with pytest.raises(NotFoundError):
        await DiscoveryEvaluationService().evaluateRun(
            dbSession, runId=999999, datasetVersion="x"
        )


async def test_record_evaluation_validates_contract(dbSession: Any) -> None:
    service = DiscoveryEvaluationService()

    with pytest.raises(ValidationError):
        await service.recordEvaluation(
            dbSession, datasetVersion="  ", metrics={}, verdict="PASS"
        )
    with pytest.raises(ValidationError):
        await service.recordEvaluation(
            dbSession, datasetVersion="v1", metrics=[], verdict="PASS"  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        await service.recordEvaluation(
            dbSession, datasetVersion="v1", metrics={}, verdict="MAYBE"
        )

    row = await service.recordEvaluation(
        dbSession,
        datasetVersion="v1",
        metrics={"candidateCount": 3},
        verdict="pass",
        createdBy="tester",
    )
    assert row.verdict == "PASS"
    stored = (
        await dbSession.execute(
            select(DiscoveryEvaluation).where(DiscoveryEvaluation.id == row.id)
        )
    ).scalars().one()
    assert stored.metrics_json == {"candidateCount": 3}
