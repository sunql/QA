"""发现提案 + 四眼审核（G3-3）：真实 PG。

钉住五件事：

1. **可编译才收**：`diff_json.manifest` 必须过 `validateManifest`（v1/v2 按版本分发）
   ——「提案能被确定性校验」是它进入审核的前提；
2. **影响面自动算**：有基线发布 ⇒ 用既有 `diffManifests` 算破坏性/加性/表面变更；
   没有基线 ⇒ `impact_json` 为 NULL（不编造「无影响」）；
3. **四眼**：`reviewer == proposer` ⇒ 409（ConflictError），状态不变、无审核留痕；
4. **状态机**：DRAFT→IN_REVIEW→(APPROVED | REJECTED | 退回 DRAFT)；
5. **审批对接护栏**：`approval_task_id` 非空时必须是 `subject_type='ontology_proposal'`。
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from sqlalchemy import select

from app.domain.discovery_models import DiscoverySource
from app.domain.discovery_pipeline_models import DiscoveryProposal, DiscoveryReview
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataSource
from app.domain.semantic_manifest_schema import SCHEMA_VERSION, manifestHash
from app.domain.semantic_release_models import SemanticRelease
from app.domain.workflow_models import ApprovalTask, WorkflowDefinition, WorkflowInstance
from app.infrastructure.security.crypto import encryptApiKey
from app.services.discovery_proposal_service import DiscoveryProposalService
from app.tests._knowledge_manifest import sampleManifest


def _candidateManifest() -> dict[str, Any]:
    manifest = copy.deepcopy(sampleManifest())
    # 改一个表面字段：validateManifest 仍然通过，diff 会看到 change。
    manifest["classes"][0]["description"] = "供应商主数据（提案修订）"
    return manifest


async def _seedSource(session: Any) -> int:
    datasource = DataSource(
        name="PROPOSAL_SRC",
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
        name="提案源",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
    )
    session.add(source)
    await session.commit()
    return source.id


async def _seedBaseRelease(session: Any, version: str = "2026.11.22-base") -> int:
    manifest = sampleManifest()
    row = SemanticRelease(
        version=version,
        status="ACTIVE",
        domain_code=None,
        manifest_json=manifest,
        manifest_hash=manifestHash(manifest),
        schema_version=SCHEMA_VERSION,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row.id


async def _seedApprovalTask(session: Any, *, subjectType: str = "ontology_proposal") -> int:
    definition = WorkflowDefinition(
        domain_code="PROCUREMENT",
        capability_code="proposal-review",
        version="1",
        definition_json={},
    )
    session.add(definition)
    await session.flush()
    instance = WorkflowInstance(
        definition_id=definition.id,
        idempotency_key=f"proposal-{subjectType}",
        created_by="tester",
    )
    session.add(instance)
    await session.flush()
    task = ApprovalTask(
        instance_id=instance.id,
        subject_type=subjectType,
        subject_id="1",
        matrix_code="M1",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task.id


async def _proposalInReview(session: Any, *, proposer: str = "alice") -> DiscoveryProposal:
    sourceId = await _seedSource(session)
    service = DiscoveryProposalService()
    proposal = await service.createProposal(
        session,
        sourceId=sourceId,
        title="供应商主数据修订",
        proposalManifest=_candidateManifest(),
        proposer=proposer,
    )
    return await service.submitForReview(session, proposal.id, actor=proposer)


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


async def test_create_proposal_computes_impact_against_the_base_release(
    dbSession: Any,
) -> None:
    sourceId = await _seedSource(dbSession)
    baseId = await _seedBaseRelease(dbSession)

    proposal = await DiscoveryProposalService().createProposal(
        dbSession,
        sourceId=sourceId,
        title="供应商主数据修订",
        proposalManifest=_candidateManifest(),
        proposer="alice",
        baseReleaseId=baseId,
    )

    assert proposal.status == "DRAFT"
    assert proposal.proposer == "alice"
    assert proposal.base_release_id == baseId
    assert proposal.diff_json["kind"] == "MANIFEST_REPLACEMENT"
    assert proposal.diff_json["baseReleaseId"] == baseId
    assert proposal.diff_json["manifest"]["classes"][0]["description"] == "供应商主数据（提案修订）"
    assert proposal.impact_json is not None
    assert proposal.impact_json["changeCount"] >= 1
    assert "riskLevel" in proposal.impact_json


async def test_create_proposal_without_base_leaves_impact_null(dbSession: Any) -> None:
    sourceId = await _seedSource(dbSession)

    proposal = await DiscoveryProposalService().createProposal(
        dbSession,
        sourceId=sourceId,
        title="首个提案",
        proposalManifest=_candidateManifest(),
        proposer="alice",
    )

    assert proposal.impact_json is None


async def test_invalid_manifest_is_rejected_and_nothing_is_stored(dbSession: Any) -> None:
    sourceId = await _seedSource(dbSession)
    broken = _candidateManifest()
    broken["joins"].append(dict(broken["joins"][0]))  # joinKey 重复 ⇒ 违反契约

    with pytest.raises(ValidationError):
        await DiscoveryProposalService().createProposal(
            dbSession,
            sourceId=sourceId,
            title="坏提案",
            proposalManifest=broken,
            proposer="alice",
        )

    assert (
        await dbSession.execute(select(DiscoveryProposal))
    ).scalars().first() is None


async def test_blank_title_is_rejected(dbSession: Any) -> None:
    sourceId = await _seedSource(dbSession)

    with pytest.raises(ValidationError):
        await DiscoveryProposalService().createProposal(
            dbSession,
            sourceId=sourceId,
            title="   ",
            proposalManifest=_candidateManifest(),
            proposer="alice",
        )


async def test_unknown_base_release_is_not_found(dbSession: Any) -> None:
    sourceId = await _seedSource(dbSession)

    with pytest.raises(NotFoundError):
        await DiscoveryProposalService().createProposal(
            dbSession,
            sourceId=sourceId,
            title="提案",
            proposalManifest=_candidateManifest(),
            proposer="alice",
            baseReleaseId=999999,
        )


# ---------------------------------------------------------------------------
# 提交与审核
# ---------------------------------------------------------------------------


async def test_submit_moves_draft_to_in_review(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)

    assert proposal.status == "IN_REVIEW"


async def test_submit_requires_draft_status(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)

    with pytest.raises(ConflictError):
        await DiscoveryProposalService().submitForReview(
            dbSession, proposal.id, actor="alice"
        )


async def test_review_requires_in_review_status(dbSession: Any) -> None:
    sourceId = await _seedSource(dbSession)
    proposal = await DiscoveryProposalService().createProposal(
        dbSession,
        sourceId=sourceId,
        title="草稿",
        proposalManifest=_candidateManifest(),
        proposer="alice",
    )

    with pytest.raises(ConflictError):
        await DiscoveryProposalService().review(
            dbSession, proposal.id, reviewer="bob", decision="APPROVE"
        )


async def test_self_review_is_rejected_by_four_eyes(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)

    with pytest.raises(ConflictError):
        await DiscoveryProposalService().review(
            dbSession, proposal.id, reviewer="alice", decision="APPROVE"
        )

    row = await dbSession.get(DiscoveryProposal, proposal.id)
    assert row.status == "IN_REVIEW"
    assert (
        await dbSession.execute(
            select(DiscoveryReview).where(DiscoveryReview.proposal_id == proposal.id)
        )
    ).scalars().first() is None


async def test_approve_by_another_reviewer_marks_approved(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)

    review = await DiscoveryProposalService().review(
        dbSession, proposal.id, reviewer="bob", decision="APPROVE", comment="同意"
    )

    assert review.reviewer == "bob"
    assert review.decision == "APPROVE"
    row = await dbSession.get(DiscoveryProposal, proposal.id)
    assert row.status == "APPROVED"


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("REJECT", "REJECTED"), ("REQUEST_CHANGES", "DRAFT")],
)
async def test_other_decisions_update_the_status(
    dbSession: Any, decision: str, expected: str
) -> None:
    proposal = await _proposalInReview(dbSession)

    await DiscoveryProposalService().review(
        dbSession, proposal.id, reviewer="bob", decision=decision
    )

    row = await dbSession.get(DiscoveryProposal, proposal.id)
    assert row.status == expected


async def test_invalid_decision_is_rejected(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)

    with pytest.raises(ValidationError):
        await DiscoveryProposalService().review(
            dbSession, proposal.id, reviewer="bob", decision="LGTM"
        )


# ---------------------------------------------------------------------------
# 审批任务对接（二期 P5）
# ---------------------------------------------------------------------------


async def test_unknown_approval_task_is_rejected(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)

    with pytest.raises(ValidationError):
        await DiscoveryProposalService().review(
            dbSession,
            proposal.id,
            reviewer="bob",
            decision="APPROVE",
            approvalTaskId=999999,
        )


async def test_approval_task_must_be_an_ontology_proposal(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)
    taskId = await _seedApprovalTask(dbSession, subjectType="purchase_order")

    with pytest.raises(ValidationError):
        await DiscoveryProposalService().review(
            dbSession,
            proposal.id,
            reviewer="bob",
            decision="APPROVE",
            approvalTaskId=taskId,
        )


async def test_valid_approval_task_is_recorded(dbSession: Any) -> None:
    proposal = await _proposalInReview(dbSession)
    taskId = await _seedApprovalTask(dbSession)

    review = await DiscoveryProposalService().review(
        dbSession,
        proposal.id,
        reviewer="bob",
        decision="APPROVE",
        approvalTaskId=taskId,
    )

    assert review.approval_task_id == taskId
