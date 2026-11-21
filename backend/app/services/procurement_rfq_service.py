"""M5-1 询价草稿服务（DRAFT-only）。

幂等（DoD 3）：`Idempotency-Key` + payload SHA-256——同 key 同 hash ⇒ 返回既有
草稿；同 key 异 hash ⇒ 409。状态迁移全部经 `procurement_rfq_state_machine`
断言（D-4：SENDING/SENT 在 M5 不可达）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.domain.models import EntityMapping
from app.domain.procurement_rfq_models import (
    ProcurementRfqDecision,
    ProcurementRfqDraft,
)
from app.services.procurement_rfq_state_machine import (
    assertRfqTransition,
)
from app.services.workflow_store import WorkflowStore


def computePayloadHash(payload: dict) -> str:
    """payload 的 canonical SHA-256（字段名排序，语义等价判定）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


#: 审批决策的**角色白名单**（计划 5.3 权限闭环；角色码对齐 RBAC seed——
#: admin 为内置超管；procurement-approver = 采购审批角色，seed 另批落）。
_APPROVER_ROLES = frozenset({"admin", "procurement-approver"})


def _assertApproverRoles(roles: Sequence[str] | None) -> None:
    """角色闸（fail-closed）：无白名单角色 ⇒ PermissionDeniedError。"""
    granted = set(roles or ())
    if not (granted & _APPROVER_ROLES):
        raise PermissionDeniedError(
            "审批决策需要 admin / procurement-approver 角色"
        )


async def _appendRfqOutbox(
    session: AsyncSession, *, rfqDraftId, decision: str, actor: str
) -> None:
    """决策事件写 P5 outbox（**经存储层**，同事务；instance_id=None——RFQ 聚合外置）。"""
    await WorkflowStore().enqueueEvent(
        session,
        instanceId=None,
        eventType="rfq.decision",
        payload={
            "rfqDraftId": str(rfqDraftId),
            "decision": decision,
            "actor": actor,
        },
    )


class ProcurementRfqService:
    """询价草稿服务（DRAFT-only：审批闭环在 M5-2）。"""

    async def createDraft(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        deadline: datetime,
        payload: dict,
        idempotencyKey: str,
        actor: str,
        notes: str | None = None,
    ) -> ProcurementRfqDraft:
        """创建询价草稿（幂等：同 key 同 payload ⇒ 返回既有；异 payload ⇒ 409）。"""
        draft, _created = await self.createDraftWithStatus(
            session, request_id=request_id, deadline=deadline, payload=payload,
            idempotencyKey=idempotencyKey, actor=actor, notes=notes,
        )
        return draft

    async def createDraftWithStatus(
        self,
        session: AsyncSession,
        *,
        request_id: str,
        deadline: datetime,
        payload: dict,
        idempotencyKey: str,
        actor: str,
        notes: str | None = None,
    ) -> tuple[ProcurementRfqDraft, bool]:
        """创建询价草稿，返回 ``(草稿, 是否本次新建)``（幂等语义同 createDraft）。"""
        if not request_id.strip():
            raise ValidationError("request_id 必填")
        payloadHash = computePayloadHash(payload)
        existing = (
            await session.execute(
                select(ProcurementRfqDraft).where(
                    ProcurementRfqDraft.idempotency_key == idempotencyKey
                )
            )
        ).scalars().first()
        if existing is not None:
            if existing.payload_hash != payloadHash:
                raise ConflictError(
                    f"Idempotency-Key {idempotencyKey} 已被不同 payload 使用（409）"
                )
            return existing, False
        draft = ProcurementRfqDraft(
            rfq_draft_id=uuid4(),
            version=1,
            status="DRAFT",
            request_id=request_id,
            deadline=deadline,
            notes=notes,
            payload_json=payload,
            payload_hash=payloadHash,
            idempotency_key=idempotencyKey,
            created_by=actor,
        )
        session.add(draft)
        await session.flush()
        return draft, True

    async def getDraft(
        self, session: AsyncSession, rfqDraftId
    ) -> ProcurementRfqDraft:
        draft = (
            await session.execute(
                select(ProcurementRfqDraft).where(
                    ProcurementRfqDraft.rfq_draft_id == rfqDraftId
                )
            )
        ).scalars().first()
        if draft is None:
            raise NotFoundError(f"询价草稿 {rfqDraftId} 不存在")
        return draft

    async def validateDraft(
        self,
        session: AsyncSession,
        rfqDraftId,
        *,
        actor: str,
        expectedVersion: int | None = None,
    ) -> ProcurementRfqDraft:
        """草稿校验（DRAFT 态）：必填 + 供应商存在性（entity_mapping）。

        通过 ⇒ 驻留 **VALIDATING**（「已通过校验、待提交」——M5-2 submit 从此态
        进 PENDING_APPROVAL）；不通过 ⇒ NEEDS_CORRECTION。
        """
        draft = await self.getDraft(session, rfqDraftId)
        if expectedVersion is not None and draft.version != expectedVersion:
            raise ConflictError(
                f"版本冲突：期望 {expectedVersion}，实际 {draft.version}"
            )
        # 两步迁移：DRAFT→VALIDATING（校验开始）→ 结果态
        #（通过 ⇒ **驻留 VALIDATING** = 「已通过、待提交」；不通过 ⇒ NEEDS_CORRECTION。
        #  状态机逐边断言。）
        if draft.status == "DRAFT":
            assertRfqTransition("DRAFT", "VALIDATING")
            draft.status = "VALIDATING"
        problems = await self._validationProblems(session, draft)
        if problems:
            assertRfqTransition("VALIDATING", "NEEDS_CORRECTION")
            draft.version += 1
            draft.status = "NEEDS_CORRECTION"
        else:
            # 校验通过 ⇒ 驻留 VALIDATING（版本仍推进：校验动作可追溯）
            draft.version += 1
        await session.flush()
        await session.refresh(draft)
        return draft

    async def cancelDraft(
        self,
        session: AsyncSession,
        rfqDraftId,
        *,
        actor: str,
        expectedVersion: int | None = None,
        roles: Sequence[str] | None = None,
    ) -> ProcurementRfqDraft:
        draft = await self.getDraft(session, rfqDraftId)
        if expectedVersion is not None and draft.version != expectedVersion:
            raise ConflictError(
                f"版本冲突：期望 {expectedVersion}，实际 {draft.version}"
            )
        assertRfqTransition(draft.status, "CANCELLED")
        draft.version += 1
        draft.status = "CANCELLED"
        await _appendRfqOutbox(
            session, rfqDraftId=draft.rfq_draft_id, decision="CANCEL", actor=actor
        )
        await session.flush()
        await session.refresh(draft)
        return draft

    async def submitForApproval(
        self,
        session: AsyncSession,
        rfqDraftId,
        *,
        actor: str,
        roles: Sequence[str] | None = None,
    ) -> ProcurementRfqDraft:
        """提交审批：**必须已通过校验**（VALIDATING 态）⇒ PENDING_APPROVAL。

        幂等：已在 PENDING_APPROVAL ⇒ no-op 重放；DRAFT（未 validate）⇒ 409
        （先校验再审批——DoD 2 的前置）。
        """
        draft = await self.getDraft(session, rfqDraftId)
        if draft.status == "PENDING_APPROVAL":
            return draft  # 幂等重放
        if draft.status != "VALIDATING":
            raise ConflictError(
                f"草稿未通过校验（当前 {draft.status}）⇒ 先 validate 再提交审批"
            )
        assertRfqTransition(draft.status, "PENDING_APPROVAL")
        draft.status = "PENDING_APPROVAL"
        session.add(
            ProcurementRfqDecision(
                rfq_draft_id=draft.rfq_draft_id, decision="SUBMIT",
                actor=actor, decided_time=_now(),
            )
        )
        await _appendRfqOutbox(
            session, rfqDraftId=draft.rfq_draft_id, decision="SUBMIT", actor=actor
        )
        await session.flush()
        await session.refresh(draft)
        return draft

    async def decideApproval(
        self,
        session: AsyncSession,
        rfqDraftId,
        *,
        decision: str,
        actor: str,
        note: str | None = None,
        roles: Sequence[str] | None = None,
    ) -> ProcurementRfqDraft:
        """审批决策：APPROVE ⇒ READY_TO_SEND；REJECT ⇒ REJECTED。

        **自批禁止**（决策人 = 提交人 ⇒ 409）；终态再决策 ⇒ 409（状态机）。
        """
        _assertApproverRoles(roles)
        if decision not in ("APPROVE", "REJECT"):
            raise ValidationError(f"非法决策：{decision}（仅 APPROVE/REJECT）")
        draft = await self.getDraft(session, rfqDraftId)
        if draft.status != "PENDING_APPROVAL":
            raise ConflictError(
                f"草稿状态 {draft.status} 不在审批中 ⇒ 不可决策（重复决策/终态）"
            )
        decisions = await self.listDecisions(session, rfqDraftId)
        submitters = {x.actor for x in decisions if x.decision == "SUBMIT"}
        if actor in submitters:
            raise ConflictError("自批禁止：决策人不得为提交人")
        target = "READY_TO_SEND" if decision == "APPROVE" else "REJECTED"
        assertRfqTransition(draft.status, target)
        draft.status = target
        session.add(
            ProcurementRfqDecision(
                rfq_draft_id=draft.rfq_draft_id, decision=decision,
                actor=actor, note=note, decided_time=_now(),
            )
        )
        await _appendRfqOutbox(
            session, rfqDraftId=draft.rfq_draft_id, decision=decision, actor=actor
        )
        await session.flush()
        await session.refresh(draft)
        return draft

    async def listDecisions(
        self, session: AsyncSession, rfqDraftId
    ) -> list[ProcurementRfqDecision]:
        """决策链（按时间序；DoD 4 审计）。"""
        return list(
            (
                await session.execute(
                    select(ProcurementRfqDecision)
                    .where(ProcurementRfqDecision.rfq_draft_id == rfqDraftId)
                    .order_by(ProcurementRfqDecision.id)
                )
            ).scalars().all()
        )

    async def listDrafts(
        self, session: AsyncSession, *, createdBy: str | None = None,
        limit: int = 50,
    ) -> list[ProcurementRfqDraft]:
        stmt = select(ProcurementRfqDraft).order_by(
            ProcurementRfqDraft.created_time.desc()
        ).limit(max(1, min(limit, 100)))
        if createdBy:
            stmt = stmt.where(ProcurementRfqDraft.created_by == createdBy)
        return list(
            (await session.execute(stmt)).scalars().all()
        )

    async def _validationProblems(
        self, session: AsyncSession, draft: ProcurementRfqDraft
    ) -> list[str]:
        problems: list[str] = []
        payload = draft.payload_json or {}
        supplierKeys = payload.get("supplierKeys") or []
        if not supplierKeys:
            problems.append("supplierKeys 为空（询价至少邀请一家）")
        if not payload.get("lineItems"):
            problems.append("lineItems 为空")
        deadline = draft.deadline
        if deadline <= _now():
            problems.append("deadline 已过期")
        # 供应商存在性（entity_mapping）
        for key in supplierKeys:
            exists = (
                await session.execute(
                    select(EntityMapping.id).where(
                        EntityMapping.entity_type == "SUPPLIER",
                        EntityMapping.enterprise_key == key,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                problems.append(f"供应商 {key} 不在统一实体映射中")
        return problems
