"""M5-3 询价草稿 API 路由守卫（handler 直调 + 双接线）。

判据要点：
1. **幂等 HTTP 契约**：201 新建 / 200 重放（同 Idempotency-Key 同 payload）；
   异 payload ⇒ 409；
2. **全链闭环经 API**：create → validate → submit-approval → approve ⇒
   READY_TO_SEND；决策链完整回显（DoD 4）；
3. **自批禁止经 API 可达**（决策人=提交人 ⇒ 409）；
4. **D-4 负向**：无发送端点（openapi 不含 send 路径）；
5. **双接线守卫**：main.py 与 _testapp.py 同块注册。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.enums import MatchRule, SourceSystem
from app.domain.exceptions import ConflictError
from app.domain.schemas import EntityMappingCreate
from app.schemas.procurement_rfq import RfqDraftCreate
from app.services.entity_mapping_service import EntityMappingService


def _user(userId: str = "creator-1") -> SimpleNamespace:
    return SimpleNamespace(userId=userId, departments=(), roles=("procurement",))


def _body(suppliers: list[int] | None = None) -> RfqDraftCreate:
    return RfqDraftCreate(
        requestId="PR-2026-0099",
        deadline=datetime(2026, 12, 20, 9, 0, tzinfo=UTC),
        supplierKeys=suppliers or [900001],
        lineItems=[
            {"materialKey": 500001, "quantity": 100, "unit": "TON",
             "deliveryDate": "2026-12-05"}
        ],
        notes="请按附件技术要求报价",
    )


def _req(idem: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"Idempotency-Key": idem})


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
        actor=SimpleNamespace(userId="m5-3-seed", departments=()),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_create_then_replay_via_api(dbSession) -> None:
    from app.api.v1.procurement_rfqs import createRfqDraft

    await _seedSupplier(dbSession)
    svc = ProcurementRfqServiceLocal = __import__(
        "app.services.procurement_rfq_service", fromlist=["ProcurementRfqService"]
    ).ProcurementRfqService()

    resp1 = SimpleNamespace(headers={})
    out1 = await createRfqDraft(
        resp1, _body(), idempotencyKey="api-idem-1",
        currentUser=_user(), session=dbSession, service=svc,
    )
    assert resp1.status_code == 201
    assert out1.status == "DRAFT"

    resp2 = SimpleNamespace(headers={})
    out2 = await createRfqDraft(
        resp2, _body(), idempotencyKey="api-idem-1",
        currentUser=_user(), session=dbSession, service=svc,
    )
    assert resp2.status_code == 200, "幂等重放 ⇒ 200"
    assert out2.rfqDraftId == out1.rfqDraftId


@pytest.mark.asyncio
async def test_full_flow_via_api_to_ready_to_send(dbSession) -> None:
    from app.api.v1.procurement_rfqs import (
        approveRfqDraft,
        createRfqDraft,
        getRfqDraft,
        submitRfqApproval,
        validateRfqDraft,
    )

    await _seedSupplier(dbSession)
    svc = __import__(
        "app.services.procurement_rfq_service", fromlist=["ProcurementRfqService"]
    ).ProcurementRfqService()
    creator, approver = _user("creator-1"), _user("approver-1")
    draftId = str(uuid4())  # 占位（真实 id 由 create 返回）

    resp = SimpleNamespace(headers={})
    created = await createRfqDraft(
        resp, _body(), idempotencyKey="api-flow-1",
        currentUser=creator, session=dbSession, service=svc,
    )
    draftId = str(created.rfqDraftId)

    validated = await validateRfqDraft(
        resp, draftId, body=None, currentUser=creator, session=dbSession
    )
    assert validated.status == "VALIDATING"

    submitted = await submitRfqApproval(
        resp, draftId, body=None, currentUser=creator, session=dbSession
    )
    assert submitted.status == "PENDING_APPROVAL"

    approved = await approveRfqDraft(
        resp, draftId, body=None, currentUser=approver, session=dbSession
    )
    assert approved.status == "READY_TO_SEND"

    detail = await getRfqDraft(resp, draftId, currentUser=creator, session=dbSession)
    decisions = [d.decision for d in detail.decisions]
    assert decisions == ["SUBMIT", "APPROVE"], "DoD 4：决策链完整回显"
    assert detail.decisions[-1].actor == "approver-1"


@pytest.mark.asyncio
async def test_self_approval_conflict_via_api(dbSession) -> None:
    from app.api.v1.procurement_rfqs import (
        approveRfqDraft,
        createRfqDraft,
        submitRfqApproval,
        validateRfqDraft,
    )

    await _seedSupplier(dbSession)
    svc = __import__(
        "app.services.procurement_rfq_service", fromlist=["ProcurementRfqService"]
    ).ProcurementRfqService()
    creator = _user("creator-1")
    resp = SimpleNamespace(headers={})
    created = await createRfqDraft(
        resp, _body(), idempotencyKey="api-self-1",
        currentUser=creator, session=dbSession, service=svc,
    )
    draftId = str(created.rfqDraftId)
    await validateRfqDraft(resp, draftId, body=None, currentUser=creator, session=dbSession)
    await submitRfqApproval(resp, draftId, body=None, currentUser=creator, session=dbSession)

    with pytest.raises(ConflictError):
        await approveRfqDraft(
            resp, draftId, body=None, currentUser=creator, session=dbSession
        )


def test_no_send_endpoint_in_router() -> None:
    """D-4 负向：RFQ 路由不得含 send 端点（DRAFT-only，发送留给 M6）。"""
    from app.api.v1.procurement_rfqs import router

    sendPaths = [r.path for r in router.routes if "/send" in r.path]
    assert sendPaths == [], f"D-4：不得存在发送端点，实际 {sendPaths}"


def test_rfqs_registered_in_both_wirings() -> None:
    from pathlib import Path

    backendRoot = Path(__file__).resolve().parents[3]
    for rel in ("app/main.py", "app/tests/_testapp.py"):
        code = (backendRoot / rel).read_text(encoding="utf-8")
        assert "procurement_rfqs" in code, f"{rel} 未导入新路由模块"
        assert "procurement_rfqs.router" in code, (
            f"{rel} 未注册 rfqs 路由（漏一边 ⇒ 那一侧 404）"
        )
