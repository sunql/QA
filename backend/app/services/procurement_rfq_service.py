"""M5-1 询价草稿服务（DRAFT-only）。

幂等（DoD 3）：`Idempotency-Key` + payload SHA-256——同 key 同 hash ⇒ 返回既有
草稿；同 key 异 hash ⇒ 409。状态迁移全部经 `procurement_rfq_state_machine`
断言（D-4：SENDING/SENT 在 M5 不可达）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import EntityMapping
from app.domain.procurement_rfq_models import ProcurementRfqDraft
from app.services.procurement_rfq_state_machine import (
    assertRfqTransition,
)


def computePayloadHash(payload: dict) -> str:
    """payload 的 canonical SHA-256（字段名排序，语义等价判定）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


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
            return existing
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
        return draft

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

        通过 ⇒ 留 DRAFT（版本推进，可追溯）；不通过 ⇒ NEEDS_CORRECTION。
        提交审批在 M5-2（本批不创建业务命令）。
        """
        draft = await self.getDraft(session, rfqDraftId)
        if expectedVersion is not None and draft.version != expectedVersion:
            raise ConflictError(
                f"版本冲突：期望 {expectedVersion}，实际 {draft.version}"
            )
        # 两步迁移：DRAFT→VALIDATING（校验开始）→ 结果态
        #（通过回落 DRAFT / 不通过 NEEDS_CORRECTION；状态机逐边断言）。
        if draft.status == "DRAFT":
            assertRfqTransition("DRAFT", "VALIDATING")
            draft.status = "VALIDATING"
        problems = await self._validationProblems(session, draft)
        target = "NEEDS_CORRECTION" if problems else "DRAFT"
        assertRfqTransition("VALIDATING", target)
        draft.version += 1
        draft.status = target
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
    ) -> ProcurementRfqDraft:
        draft = await self.getDraft(session, rfqDraftId)
        if expectedVersion is not None and draft.version != expectedVersion:
            raise ConflictError(
                f"版本冲突：期望 {expectedVersion}，实际 {draft.version}"
            )
        assertRfqTransition(draft.status, "CANCELLED")
        draft.version += 1
        draft.status = "CANCELLED"
        await session.flush()
        await session.refresh(draft)
        return draft

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
