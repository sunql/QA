"""M5-1 询价草稿数据面单元守卫（真实 PG）。

判据要点（设计 §M5-1 + DoD）：
1. **幂等（DoD 3）**：同 Idempotency-Key + 同 payload ⇒ 返回**同一** rfqDraftId；
   同 key 异 payload ⇒ **409**（ConflictError）；
2. **状态机（D-4）**：DRAFT→VALIDATING→PENDING_APPROVAL 合法；
   READY_TO_SEND ⇒ 无出口（SENDING 在 M5 不可达）⇒ 尝试推进 ⇒ 拒绝；
   REJECTED/CANCELLED 终态 ⇒ 不可再迁；
3. **validate**：必填/供应商存在性（entity_mapping）不通过 ⇒ NEEDS_CORRECTION；
   通过 ⇒ 回 DRAFT（提交审批在 M5-2）；
4. 版本乐观锁：version 不符 ⇒ 409。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.enums import MatchRule, SourceSystem
from app.domain.exceptions import ConflictError
from app.domain.schemas import EntityMappingCreate
from app.services.entity_mapping_service import EntityMappingService
from app.services.procurement_rfq_service import (
    ProcurementRfqService,
    computePayloadHash,
)


def _payload(suppliers: list[int] | None = None) -> dict:
    return {
        "supplierKeys": suppliers or [900001],
        "lineItems": [
            {"materialKey": 500001, "quantity": 100, "unit": "TON",
             "deliveryDate": "2026-12-05"}
        ],
    }


def _createArgs(*, suppliers: list[int] | None = None, **extra) -> dict:
    """构造 createDraft 参数；suppliers 覆盖进 payload（不是顶层 kwarg）。"""
    payload = _payload(suppliers)
    return {
        "request_id": extra.pop("request_id", "PR-2026-0031"),
        "deadline": extra.pop(
            "deadline", datetime(2026, 12, 20, 9, 0, tzinfo=UTC)
        ),
        "payload": extra.pop("payload", payload),
        "notes": extra.pop("notes", "请按附件技术要求报价"),
        **extra,
    }


def _hash(payload: dict) -> str:
    return computePayloadHash(payload)


async def _seedSupplier(dbSession) -> None:
    await EntityMappingService().createMapping(
        dbSession,
        EntityMappingCreate(
            entity_type="SUPPLIER",
            enterprise_key=900001,
            enterprise_code="SUP001",
            source_system=SourceSystem.ERP,
            source_key="ERP-900001",
            source_code="ERP-SUP001",
            match_rule=MatchRule.MDM_MASTER,
            name="泰鸿钢铁",
        ),
        actor=SimpleNamespaceActor(),  # type: ignore[arg-type]
    )


class SimpleNamespaceActor:
    userId = "m5-1-test"
    departments: tuple = ()


# ── 1. 创建 + 幂等（DoD 3）────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_draft_with_idempotent_key(dbSession) -> None:
    svc = ProcurementRfqService()
    args = _createArgs()
    d1 = await svc.createDraft(dbSession, **args, idempotencyKey="idem-1", actor="m5-1-test")
    assert d1.status == "DRAFT"
    assert d1.payload_hash == _hash(args["payload"])
    assert d1.version == 1
    assert d1.created_by == "m5-1-test"


@pytest.mark.asyncio
async def test_same_key_same_payload_returns_same_draft(dbSession) -> None:
    svc = ProcurementRfqService()
    args = _createArgs()
    d1 = await svc.createDraft(dbSession, **args, idempotencyKey="idem-2", actor="a")
    d2 = await svc.createDraft(dbSession, **args, idempotencyKey="idem-2", actor="a")
    assert d1.rfq_draft_id == d2.rfq_draft_id, (
        "同 key + 同 payload ⇒ 返回同一草稿（DoD 3）"
    )


@pytest.mark.asyncio
async def test_same_key_different_payload_conflicts(dbSession) -> None:
    svc = ProcurementRfqService()
    await svc.createDraft(
        dbSession, **_createArgs(), idempotencyKey="idem-3", actor="a"
    )
    with pytest.raises(ConflictError):
        await svc.createDraft(
            dbSession,
            **_createArgs(suppliers=[900002]),
            idempotencyKey="idem-3",
            actor="a",
        )


# ── 2. 状态机（D-4）────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_pass_keeps_draft_state(dbSession) -> None:
    await _seedSupplier(dbSession)
    svc = ProcurementRfqService()
    d = await svc.createDraft(
        dbSession, **_createArgs(), idempotencyKey="idem-4", actor="a"
    )
    result = await svc.validateDraft(dbSession, d.rfq_draft_id, actor="a")
    assert result.status == "DRAFT", "校验通过 ⇒ 留 DRAFT（提交审批在 M5-2）"
    assert result.version == 2, "validate 落版本推进（可追溯）"


@pytest.mark.asyncio
async def test_validate_unknown_supplier_goes_needs_correction(dbSession) -> None:
    svc = ProcurementRfqService()
    payload = _payload(suppliers=[999999])  # 未 seed ⇒ 不存在
    d = await svc.createDraft(
        dbSession, **_createArgs(payload=payload), idempotencyKey="idem-5", actor="a"
    )
    result = await svc.validateDraft(dbSession, d.rfq_draft_id, actor="a")
    assert result.status == "NEEDS_CORRECTION"


@pytest.mark.asyncio
async def test_ready_to_send_has_no_outbound_transition_d4(dbSession) -> None:
    """D-4 负向用例：READY_TO_SEND 在 M5 无出口（SENDING 不可达）。"""
    from app.services.procurement_rfq_state_machine import (
        allowedRfqTransitions,
        assertRfqTransition,
    )

    assert allowedRfqTransitions("READY_TO_SEND") == frozenset({"CANCELLED"})
    with pytest.raises(ValueError):
        assertRfqTransition("READY_TO_SEND", "SENDING")
    with pytest.raises(ValueError):
        assertRfqTransition("READY_TO_SEND", "SENT")


@pytest.mark.asyncio
async def test_terminal_states_are_immutable() -> None:
    from app.services.procurement_rfq_state_machine import assertRfqTransition

    for terminal in ("REJECTED", "CANCELLED", "CLOSED"):
        with pytest.raises(ValueError):
            assertRfqTransition(terminal, "DRAFT")


# ── 3. 版本乐观锁 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transition_with_stale_version_conflicts(dbSession) -> None:
    await _seedSupplier(dbSession)
    svc = ProcurementRfqService()
    d = await svc.createDraft(
        dbSession, **_createArgs(), idempotencyKey="idem-6", actor="a"
    )
    with pytest.raises(ConflictError):
        await svc.validateDraft(
            dbSession, d.rfq_draft_id, actor="a", expectedVersion=99
        )
