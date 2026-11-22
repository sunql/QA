"""发现提案 + 四眼审核（WP-ONT-AI A2 / G3-3）。

提案 = 「候选之后的完整 manifest + 基线 + 影响面」，审核 = 四眼留痕。

## 为什么 `diff_json` 存**完整 manifest** 而不是补丁

补丁无法独立校验：「两个分区之间的引用是否正确」只有对**完整快照**跑
`validateManifest` 才知道（v1/v2 按 `schemaVersion` 分发）。所以提案载荷是

```json
{"kind": "MANIFEST_REPLACEMENT", "baseReleaseId": 7, "manifest": {...v2 快照...}}
```

`impact_json` 才是「相对基线变了什么」（用既有 `diffManifests` 算，不另造差异口径）。

## 状态机

```
DRAFT --submit--> IN_REVIEW --APPROVE--> APPROVED
                             --REJECT--> REJECTED
                             --REQUEST_CHANGES--> DRAFT（退回提案人，可改后重提）
APPROVED --(G4 publish)--> PUBLISHED
```

## 四眼与审批对接

- `reviewer == proposer` ⇒ **409**（同人自审不是审核）。这是**跨表**约束，库层 CHECK
  表达不了 ⇒ 服务层拒绝，单测钉住。
- `approval_task_id` 允许为空（首期轻量审核）；非空时该任务必须是
  `subject_type='ontology_proposal'`（二期接 P5 审批时回填，防「随便挂一个审批任务」）。
- **本服务不写 `semantic_release`**：批准只把状态推到 `APPROVED` 并留给 G4 的
  publish（唯一写口 = 既有 `semanticReleaseService`）。发现域的红线是「不写治理表」，
  在这里体现为「只留指针、不建发布」。
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_models import DiscoverySource
from app.domain.discovery_pipeline_models import DiscoveryProposal, DiscoveryReview
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.semantic_manifest_schema import (
    ManifestSchemaError,
    validateManifest,
)
from app.domain.semantic_release_models import SemanticRelease
from app.domain.workflow_models import ApprovalTask
from app.services.semantic_diff_service import diffManifests

logger = logging.getLogger(__name__)

STATUS_DRAFT = "DRAFT"
STATUS_IN_REVIEW = "IN_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_PUBLISHED = "PUBLISHED"

PROPOSAL_KIND_MANIFEST_REPLACEMENT = "MANIFEST_REPLACEMENT"
APPROVAL_SUBJECT_ONTOLOGY_PROPOSAL = "ontology_proposal"

DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"
DECISION_REQUEST_CHANGES = "REQUEST_CHANGES"
DECISIONS = frozenset({DECISION_APPROVE, DECISION_REJECT, DECISION_REQUEST_CHANGES})

_DECISION_STATUS = {
    DECISION_APPROVE: STATUS_APPROVED,
    DECISION_REJECT: STATUS_REJECTED,
    DECISION_REQUEST_CHANGES: STATUS_DRAFT,
}

MSG_SOURCE_NOT_FOUND = "发现源 {id} 不存在"
MSG_TITLE_REQUIRED = "提案标题不能为空（最多 200 字）"
MSG_MANIFEST_INVALID = "提案 manifest 不满足契约：{detail}"
MSG_BASE_RELEASE_NOT_FOUND = "基线发布 {id} 不存在"
MSG_PROPOSAL_NOT_FOUND = "提案 {id} 不存在"
MSG_SUBMIT_REQUIRES_DRAFT = "提案 {id} 当前状态 {status}，只有 DRAFT 可以提交审核"
MSG_REVIEW_REQUIRES_IN_REVIEW = "提案 {id} 当前状态 {status}，只有 IN_REVIEW 可以审核"
MSG_SELF_REVIEW = "不能审核自己提交的提案（四眼原则）"
MSG_DECISION_INVALID = "审核结论非法：{decision}（仅允许 APPROVE / REJECT / REQUEST_CHANGES）"
MSG_APPROVAL_TASK_NOT_FOUND = "审批任务 {id} 不存在"
MSG_APPROVAL_TASK_SUBJECT_INVALID = (
    "审批任务 {id} 的 subject_type 为 {subjectType}，必须指向 ontology_proposal"
)

MAX_TITLE_LENGTH = 200


@dataclass(frozen=True, slots=True)
class ProposalImpact:
    """影响面摘要（落 `impact_json`）。"""

    changeCount: int
    breakingCount: int
    additiveCount: int
    cosmeticCount: int
    riskLevel: str
    sections: dict[str, int]

    def asJson(self) -> dict[str, Any]:
        return {
            "changeCount": self.changeCount,
            "breakingCount": self.breakingCount,
            "additiveCount": self.additiveCount,
            "cosmeticCount": self.cosmeticCount,
            "riskLevel": self.riskLevel,
            "sections": self.sections,
        }


class DiscoveryProposalService:
    """提案与审核（只写 `discovery_proposal` / `discovery_review`）。"""

    async def createProposal(
        self,
        session: AsyncSession,
        *,
        sourceId: int,
        title: str,
        proposalManifest: Mapping[str, Any],
        proposer: str,
        baseReleaseId: int | None = None,
        note: str | None = None,
    ) -> DiscoveryProposal:
        """建提案草稿：manifest 必须过契约；有基线则算影响面。"""
        normalizedTitle = (title or "").strip()
        if not normalizedTitle or len(normalizedTitle) > MAX_TITLE_LENGTH:
            raise ValidationError(MSG_TITLE_REQUIRED)
        source = await session.get(DiscoverySource, sourceId)
        if source is None:
            raise NotFoundError(MSG_SOURCE_NOT_FOUND.format(id=sourceId))
        _assertManifest(proposalManifest)
        baseManifest = await self._baseManifest(session, baseReleaseId)
        impact = (
            _impactOf(baseManifest, proposalManifest) if baseManifest is not None else None
        )
        proposal = DiscoveryProposal(
            title=normalizedTitle,
            source_id=sourceId,
            base_release_id=baseReleaseId,
            diff_json={
                "kind": PROPOSAL_KIND_MANIFEST_REPLACEMENT,
                "baseReleaseId": baseReleaseId,
                # 深拷贝：JSONB 不做可变追踪，共享嵌套 dict = 静默脏数据。
                "manifest": copy.deepcopy(dict(proposalManifest)),
                "note": note,
            },
            impact_json=impact.asJson() if impact is not None else None,
            status=STATUS_DRAFT,
            proposer=proposer,
        )
        session.add(proposal)
        await session.commit()
        await session.refresh(proposal)
        return proposal

    async def submitForReview(
        self, session: AsyncSession, proposalId: int, *, actor: str
    ) -> DiscoveryProposal:
        proposal = await self._load(session, proposalId)
        if proposal.status != STATUS_DRAFT:
            raise ConflictError(
                MSG_SUBMIT_REQUIRES_DRAFT.format(id=proposalId, status=proposal.status)
            )
        proposal.status = STATUS_IN_REVIEW
        await session.commit()
        await session.refresh(proposal)
        logger.info("提案 %s 提交审核（actor=%s）", proposalId, actor)
        return proposal

    async def review(
        self,
        session: AsyncSession,
        proposalId: int,
        *,
        reviewer: str,
        decision: str,
        comment: str | None = None,
        approvalTaskId: int | None = None,
    ) -> DiscoveryReview:
        """四眼审核：同人自审 409；结论驱动状态机。"""
        normalizedDecision = (decision or "").strip().upper()
        if normalizedDecision not in DECISIONS:
            raise ValidationError(
                MSG_DECISION_INVALID.format(decision=decision)
            )
        proposal = await self._load(session, proposalId)
        if proposal.status != STATUS_IN_REVIEW:
            raise ConflictError(
                MSG_REVIEW_REQUIRES_IN_REVIEW.format(
                    id=proposalId, status=proposal.status
                )
            )
        if reviewer == proposal.proposer:
            raise ConflictError(MSG_SELF_REVIEW)
        await self._assertApprovalTask(session, approvalTaskId)

        review = DiscoveryReview(
            proposal_id=proposalId,
            reviewer=reviewer,
            decision=normalizedDecision,
            comment=(comment or "").strip() or None,
            approval_task_id=approvalTaskId,
        )
        session.add(review)
        proposal.status = _DECISION_STATUS[normalizedDecision]
        await session.commit()
        await session.refresh(review)
        await session.refresh(proposal)
        return review

    async def getProposal(
        self, session: AsyncSession, proposalId: int
    ) -> DiscoveryProposal:
        return await self._load(session, proposalId)

    async def listReviews(
        self, session: AsyncSession, proposalId: int
    ) -> list[DiscoveryReview]:
        await self._load(session, proposalId)
        rows = await session.execute(
            select(DiscoveryReview)
            .where(DiscoveryReview.proposal_id == proposalId)
            .order_by(DiscoveryReview.id)
        )
        return list(rows.scalars().all())

    # ------------------------------------------------------------ 内部

    async def _load(
        self, session: AsyncSession, proposalId: int
    ) -> DiscoveryProposal:
        row = await session.get(DiscoveryProposal, proposalId)
        if row is None:
            raise NotFoundError(MSG_PROPOSAL_NOT_FOUND.format(id=proposalId))
        return row

    async def _baseManifest(
        self, session: AsyncSession, baseReleaseId: int | None
    ) -> Mapping[str, Any] | None:
        if baseReleaseId is None:
            return None
        base = await session.get(SemanticRelease, baseReleaseId)
        if base is None or not isinstance(base.manifest_json, Mapping):
            raise NotFoundError(MSG_BASE_RELEASE_NOT_FOUND.format(id=baseReleaseId))
        return base.manifest_json

    async def _assertApprovalTask(
        self, session: AsyncSession, approvalTaskId: int | None
    ) -> None:
        if approvalTaskId is None:
            return
        task = await session.get(ApprovalTask, approvalTaskId)
        if task is None:
            raise ValidationError(
                MSG_APPROVAL_TASK_NOT_FOUND.format(id=approvalTaskId)
            )
        if task.subject_type != APPROVAL_SUBJECT_ONTOLOGY_PROPOSAL:
            raise ValidationError(
                MSG_APPROVAL_TASK_SUBJECT_INVALID.format(
                    id=approvalTaskId, subjectType=task.subject_type
                )
            )


# ---------------------------------------------------------------------------
# 内部：契约与影响面
# ---------------------------------------------------------------------------


def _assertManifest(manifest: Mapping[str, Any]) -> None:
    """提案的「可编译」判据 = 与发布同一道契约闸（按版本分发，未知版本拒绝）。"""
    try:
        validateManifest(dict(manifest))
    except ManifestSchemaError as exc:
        raise ValidationError(MSG_MANIFEST_INVALID.format(detail=str(exc))) from exc


def _impactOf(
    base: Mapping[str, Any], candidate: Mapping[str, Any]
) -> ProposalImpact:
    diff = diffManifests(base, candidate)
    sections: dict[str, int] = {}
    for change in diff.changes:
        sections[change.section] = sections.get(change.section, 0) + 1
    return ProposalImpact(
        changeCount=diff.changeCount,
        breakingCount=diff.breakingCount,
        additiveCount=diff.additiveCount,
        cosmeticCount=diff.cosmeticCount,
        riskLevel=diff.overallRiskLevel,
        sections=dict(sorted(sections.items())),
    )
