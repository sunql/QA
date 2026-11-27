"""催货单服务：全生命周期 + 幂等 + 分组（真实 PostgreSQL，feat/delivery-chase-closed-loop）。

钉住六件事：

1. **按供应商分组**：跨供应商的选行 ⇒ 多张催货单（一个供应商一张）；
2. **服务端数据权威**：行数据来自回查结果（`lineRowsByIdentity`），
   **不是**前端传的数量；
3. **幂等**：同 (supplier, lines, deadline) ⇒ 复用既有草稿（reused=True）；
4. **生命周期**：validate → approve → send 的状态推进与决策留痕；
5. **供应商必须存在 entity_mapping**：解析不到 ⇒ 该组剔除并点名；
6. **全选行不可解析 ⇒ 明确失败**（不产出空催货单）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.domain.exceptions import ConflictError, ValidationError
from app.domain.models import EntityMapping
from app.domain.procurement_chase_models import ProcurementChaseDecision
from app.services.procurement_chase_service import (
    ProcurementChaseService,
    chaseIdempotencyKey,
)
from app.services.procurement_command_port import SimulatedErpSrmAdapter

def _deadline() -> datetime:
    """每次调用独立期限（模块级常量会让不同用例的草稿撞同一个幂等键）。"""
    return datetime.now(UTC) + timedelta(days=3)


def _line(
    poNo: str,
    poLine: str,
    supplierCode: str,
    *,
    openQty: float = 10.0,
    overdueDays: int = 5,
) -> dict[str, Any]:
    """模拟**服务端回查结果**的行（MURDO 形态 = 大写键，与 Oracle 返回一致）。"""
    return {
        "PO_NO": poNo,
        "PO_LINE_NO": poLine,
        "ITEM_CODE": "M001",
        "SUPPLIER_CODE": supplierCode,
        "ORDERED_QTY": openQty + 2,
        "RECEIVED_QTY": 2.0,
        "OPEN_QTY": openQty,
        "EXPECTED_DATE": "2026-11-20",
        "OVERDUE_DAYS": overdueDays,
    }


_SUPPLIER_KEY_SEQ = {"next": 900000001}


async def _seedSupplier(session: Any, code: str) -> int:
    """种供应商映射（确定性键：hash() 每进程不同会污染幂等键比对）。"""
    existing = (
        await session.execute(
            select(EntityMapping).where(
                EntityMapping.entity_type == "SUPPLIER",
                EntityMapping.source_system == "ERP",
                EntityMapping.source_key == code,
            )
        )
    ).scalars().first()
    if existing is not None:
        return int(existing.enterprise_key)
    _SUPPLIER_KEY_SEQ["next"] += 1
    row = EntityMapping(
        entity_type="SUPPLIER",
        enterprise_key=_SUPPLIER_KEY_SEQ["next"],
        enterprise_code=code,
        source_system="ERP",
        source_key=code,
        source_code=code,
        match_rule="MAPPING",
    )
    session.add(row)
    await session.commit()
    return int(row.enterprise_key)


def _identitiesOf(lines: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(line["PO_NO"]), str(line["PO_LINE_NO"])) for line in lines]


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------


def test_idempotency_key_is_order_insensitive() -> None:
    """同 supplier + 同行集合（不同顺序）+ 同期限 ⇒ 同键。"""
    deadline = _deadline()
    key1 = chaseIdempotencyKey(
        supplierEnterpriseKey=1,
        poLines=[("PO1", "1"), ("PO2", "2")],
        expectedReplyDeadline=deadline,
    )
    key2 = chaseIdempotencyKey(
        supplierEnterpriseKey=1,
        poLines=[("PO2", "2"), ("PO1", "1")],
        expectedReplyDeadline=deadline,
    )
    assert key1 == key2


def test_idempotency_key_differs_by_supplier_or_deadline() -> None:
    key = chaseIdempotencyKey(
        supplierEnterpriseKey=1,
        poLines=[("PO1", "1")],
        expectedReplyDeadline=_deadline(),
    )
    otherSupplier = chaseIdempotencyKey(
        supplierEnterpriseKey=2,
        poLines=[("PO1", "1")],
        expectedReplyDeadline=_deadline(),
    )
    otherDeadline = chaseIdempotencyKey(
        supplierEnterpriseKey=1,
        poLines=[("PO1", "1")],
        expectedReplyDeadline=_deadline() + timedelta(days=1),
    )
    assert key != otherSupplier
    assert key != otherDeadline


# ---------------------------------------------------------------------------
# 创建（分组 + 幂等 + 服务端数据权威）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_groups_by_supplier(dbSession: Any) -> None:
    """跨供应商选行 ⇒ 两张催货单；每张的行来自**回查数据**（数量以回查为准）。"""
    await _seedSupplier(dbSession, "Q1630")
    await _seedSupplier(dbSession, "Q202")
    service = ProcurementChaseService()
    rowsByIdentity = {
        ("PO1", "1"): _line("PO1", "1", "Q1630", openQty=10.0),
        ("PO1", "2"): _line("PO1", "2", "Q1630", openQty=20.0),
        ("PO2", "1"): _line("PO2", "1", "Q202", openQty=5.0),
    }

    result = await service.createDraftFromPending(
        dbSession,
        poLines=[{"poNo": po, "poLine": line} for po, line in rowsByIdentity],
        expectedReplyDeadline=_deadline(),
        createdBy="tester",
        lineRowsByIdentity=rowsByIdentity,
    )
    await dbSession.commit()

    assert len(result["drafts"]) == 2
    assert result["unresolvedSuppliers"] == []
    assert result["skippedLines"] == []
    codes = {item["draft"].supplier_code for item in result["drafts"]}
    assert codes == {"Q1630", "Q202"}
    q1630 = next(
        item for item in result["drafts"] if item["draft"].supplier_code == "Q1630"
    )
    assert q1630["lineCount"] == 2
    # 行数量来自回查数据（openQty=10 不是前端传的）
    payload = q1630["draft"].payload_json
    assert payload["lines"][0]["openQty"] == 10.0


@pytest.mark.asyncio
async def test_create_is_idempotent(dbSession: Any) -> None:
    """同选行 + 同期限再提交 ⇒ 复用既有草稿（reused=True），不新增。"""
    await _seedSupplier(dbSession, "Q1630")
    service = ProcurementChaseService()
    rowsByIdentity = {("PO1", "1"): _line("PO1", "1", "Q1630")}
    args = dict(
        poLines=[{"poNo": "PO1", "poLine": "1"}],
        expectedReplyDeadline=_deadline(),
        createdBy="tester",
        lineRowsByIdentity=rowsByIdentity,
    )

    first = await service.createDraftFromPending(dbSession, **args)
    second = await service.createDraftFromPending(dbSession, **args)
    await dbSession.commit()

    assert first["drafts"][0]["reused"] is False
    assert second["drafts"][0]["reused"] is True
    assert first["drafts"][0]["draft"].id == second["drafts"][0]["draft"].id


@pytest.mark.asyncio
async def test_create_reports_unresolved_supplier_and_keeps_rest(dbSession: Any) -> None:
    """一个供应商不在 entity_mapping ⇒ 该组剔除并点名；另一组照常创建。"""
    await _seedSupplier(dbSession, "Q1630")
    service = ProcurementChaseService()
    rowsByIdentity = {
        ("PO1", "1"): _line("PO1", "1", "Q1630"),
        ("PO2", "1"): _line("PO2", "1", "Q999"),
    }

    result = await service.createDraftFromPending(
        dbSession,
        poLines=[{"poNo": po, "poLine": line} for po, line in rowsByIdentity],
        expectedReplyDeadline=_deadline(),
        createdBy="tester",
        lineRowsByIdentity=rowsByIdentity,
    )
    await dbSession.commit()

    assert result["unresolvedSuppliers"] == ["Q999"]
    assert len(result["drafts"]) == 1
    assert result["drafts"][0]["draft"].supplier_code == "Q1630"


@pytest.mark.asyncio
async def test_create_fails_when_no_supplier_resolves(dbSession: Any) -> None:
    """全部供应商都解析不到 ⇒ 422（不产出空催货单）。"""
    service = ProcurementChaseService()
    rowsByIdentity = {("PO1", "1"): _line("PO1", "1", "QNOPE")}

    with pytest.raises(ValidationError, match="无法生成催货单"):
        await service.createDraftFromPending(
            dbSession,
            poLines=[{"poNo": "PO1", "poLine": "1"}],
            expectedReplyDeadline=_deadline(),
            createdBy="tester",
            lineRowsByIdentity=rowsByIdentity,
        )


@pytest.mark.asyncio
async def test_create_rejects_past_deadline(dbSession: Any) -> None:
    """答复期限早于当前 ⇒ 422。"""
    service = ProcurementChaseService()
    with pytest.raises(ValidationError, match="答复期限"):
        await service.createDraftFromPending(
            dbSession,
            poLines=[{"poNo": "PO1", "poLine": "1"}],
            expectedReplyDeadline=datetime.now(UTC) - timedelta(days=1),
            createdBy="tester",
            lineRowsByIdentity={("PO1", "1"): _line("PO1", "1", "Q1630")},
        )


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def _createOne(dbSession: Any) -> Any:
    await _seedSupplier(dbSession, "Q1630")
    service = ProcurementChaseService()
    result = await service.createDraftFromPending(
        dbSession,
        poLines=[{"poNo": "PO1", "poLine": "1"}],
        expectedReplyDeadline=_deadline(),
        createdBy="tester",
        lineRowsByIdentity={("PO1", "1"): _line("PO1", "1", "Q1630")},
    )
    await dbSession.commit()
    return service, str(result["drafts"][0]["draft"].chase_draft_id)


@pytest.mark.asyncio
async def test_lifecycle_validate_approve_send(dbSession: Any) -> None:
    """validate → approve → send 全链；每步留痕；send 后外部引用可追溯。"""
    service, chaseDraftId = await _createOne(dbSession)
    adapter = SimulatedErpSrmAdapter()

    draft = await service.validateDraft(dbSession, chaseDraftId, actor="tester")
    assert draft.status == "PENDING_APPROVAL"
    await dbSession.commit()

    draft = await service.approveDraft(dbSession, chaseDraftId, actor="boss")
    assert draft.status == "READY_TO_SEND"
    await dbSession.commit()

    draft = await service.sendDraft(dbSession, chaseDraftId, actor="boss", port=adapter)
    await dbSession.commit()
    assert draft.status == "SENT"

    detail = await service.getDraft(dbSession, chaseDraftId)
    decisions = detail["decisions"]
    assert [d.decision for d in decisions] == ["SUBMIT", "APPROVE", "SEND"]
    # 发送留痕带确定性外部引用（模拟器）
    assert decisions[-1].note.startswith("CHASE-SIM-")


@pytest.mark.asyncio
async def test_send_is_idempotent_on_external_ref(dbSession: Any) -> None:
    """同幂等键重发 ⇒ 外部引用不变（「超时重试 ⇒ 重复发送」防线）。"""
    service, chaseDraftId = await _createOne(dbSession)
    adapter = SimulatedErpSrmAdapter()
    await service.validateDraft(dbSession, chaseDraftId, actor="tester")
    await dbSession.commit()
    await service.approveDraft(dbSession, chaseDraftId, actor="boss")
    await dbSession.commit()
    first = await service.sendDraft(dbSession, chaseDraftId, actor="boss", port=adapter)
    await dbSession.commit()
    ref1 = (
        await dbSession.execute(
            select(ProcurementChaseDecision.note)
            .where(ProcurementChaseDecision.chase_draft_id == first.chase_draft_id)
            .order_by(ProcurementChaseDecision.id.desc())
            .limit(1)
        )
    ).scalar_one()

    # 模拟「重发同一张」（真实实现里 status 已 SENT ⇒ 不可再发；这里验证端口幂等）
    portResult = await adapter.sendChase(first.payload_json, idempotencyKey=first.idempotency_key)
    assert portResult.externalRef == ref1


@pytest.mark.asyncio
async def test_reject_and_cancel_paths(dbSession: Any) -> None:
    """REJECTED / CANCELLED 都是可达终态。"""
    service, chaseDraftId = await _createOne(dbSession)
    await service.validateDraft(dbSession, chaseDraftId, actor="tester")
    await dbSession.commit()
    draft = await service.rejectDraft(dbSession, chaseDraftId, actor="boss", note="数量不对")
    await dbSession.commit()
    assert draft.status == "REJECTED"

    # CANCELLED 走独立草稿（REJECTED 已是终态，不能再 cancel —— 状态机负向钉住）
    service2, chaseDraftId2 = await _createOne(dbSession)
    draft2 = await service2.cancelDraft(dbSession, chaseDraftId2, actor="tester")
    await dbSession.commit()
    assert draft2.status == "CANCELLED"

    # 终态不可再迁移（fail-closed）⇒ 对外 409（ConflictError）
    with pytest.raises(ConflictError, match="非法状态迁移"):
        await service.cancelDraft(dbSession, chaseDraftId, actor="tester")
