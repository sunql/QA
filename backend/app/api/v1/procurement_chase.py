"""催货单 API（feat/delivery-chase-closed-loop）。

路由挂载：`/api/v1/procurement/chase`（admin）。

  POST /from-pending              Copilot 选行 → 批量创建（按供应商分组）
  GET  /drafts                    列表
  GET  /drafts/{chaseDraftId}     详情（含行 + 决策时间线）
  POST /drafts/{id}/validate      → PENDING_APPROVAL | NEEDS_CORRECTION
  POST /drafts/{id}/submit-approval → PENDING_APPROVAL（validate 已直达 ⇒ 保留别名）
  POST /drafts/{id}/approve       → READY_TO_SEND
  POST /drafts/{id}/reject        → REJECTED（终态）
  POST /drafts/{id}/cancel        → CANCELLED（终态）
  POST /drafts/{id}/send          → SENDING → SENT（经命令端口；本期终态）

权限：admin（对外动作）；限流走 `rateLimitDep`；动作写 audit_log（service 层同事务
决策留痕已有，这里再记操作审计）。
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getDb, requireAdmin
from app.domain.exceptions import DomainError
from app.schemas.procurement_chase import (
    ChaseDecisionBody,
    ChaseDecisionRead,
    ChaseDraftDetailRead,
    ChaseDraftRead,
    ChaseFromPendingCreate,
    ChaseFromPendingResultRead,
    ChaseLineRead,
)
from app.services.procurement_chase_lookup import fetchPendingLinesEnriched
from app.services.procurement_chase_service import (
    ProcurementChaseService,
    defaultReplyDeadline,
)

router = APIRouter(tags=["procurement-chase"])

_service = ProcurementChaseService()


def _noStore(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _draftRead(draft) -> ChaseDraftRead:
    return ChaseDraftRead(
        chase_draft_id=str(draft.chase_draft_id),
        version=draft.version,
        status=draft.status,
        request_id=draft.request_id,
        supplier_enterprise_key=draft.supplier_enterprise_key,
        supplier_code=draft.supplier_code,
        expected_reply_deadline=draft.expected_reply_deadline,
        notes=draft.notes,
        line_count=len(draft.payload_json.get("lines") or []),
        created_by=draft.created_by,
        created_time=draft.created_time,
        updated_time=draft.updated_time,
    )


def _detailRead(detail: dict) -> ChaseDraftDetailRead:
    draft = detail["draft"]
    return ChaseDraftDetailRead(
        draft=_draftRead(draft),
        lines=[
            ChaseLineRead(
                po_no=line.po_no,
                po_line=line.po_line_no,
                material_code=line.material_code,
                material_desc=line.material_desc,
                ordered_qty=line.ordered_qty,
                received_qty=line.received_qty,
                open_qty=line.open_qty,
                expected_date=(
                    line.expected_date.isoformat() if line.expected_date else None
                ),
                overdue_days=line.overdue_days,
                is_dirty=bool(line.is_dirty),
            )
            for line in detail["lines"]
        ],
        decisions=[
            ChaseDecisionRead(
                decision=d.decision,
                actor=d.actor,
                note=d.note,
                decided_time=d.decided_time,
            )
            for d in detail["decisions"]
        ],
    )


@router.post(
    "/from-pending",
    response_model=ChaseFromPendingResultRead,
    status_code=201,
    summary="从待到货明细批量创建催货单（按供应商分组）",
)
async def createChaseFromPending(
    payload: ChaseFromPendingCreate,
    response: Response,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseFromPendingResultRead:
    """核心入口：Copilot 勾选的未到货行 → 按供应商生成催货单（可多张）。

    **服务端回查**：只信 `{poNo, poLine}` 身份对，数量/日期一律回查业务库。
    """
    _noStore(response)
    deadline = payload.expected_reply_deadline or defaultReplyDeadline()
    # 统一时区语义（无 tz 视为 UTC；前端传 ISO 即带 tz）
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    identities = [(line.po_no, line.po_line) for line in payload.lines]
    try:
        lineRows = await fetchPendingLinesEnriched(session, identities=identities)
    except DomainError:
        raise
    except Exception as exc:  # noqa: BLE001 - 回查失败统一友好文案
        raise DomainError(
            "回查待到货行失败：请稍后重试或联系管理员", detail=str(exc)
        ) from exc
    result = await _service.createDraftFromPending(
        session,
        poLines=[{"poNo": po, "poLine": line} for po, line in identities],
        expectedReplyDeadline=deadline,
        createdBy=admin.userId,
        notes=payload.notes,
        lineRowsByIdentity=lineRows,
    )
    await session.commit()
    return ChaseFromPendingResultRead(
        request_id=result["requestId"],
        drafts=[_draftRead(item["draft"]) for item in result["drafts"]],
        unresolved_suppliers=result["unresolvedSuppliers"],
        skipped_lines=result["skippedLines"],
    )


@router.get("/drafts", response_model=list[ChaseDraftRead], summary="催货单列表")
async def listChaseDrafts(
    response: Response,
    _user: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> list[ChaseDraftRead]:
    _noStore(response)
    drafts = await _service.listDrafts(session)
    return [_draftRead(draft) for draft in drafts]


@router.get(
    "/drafts/{chaseDraftId}",
    response_model=ChaseDraftDetailRead,
    summary="催货单详情（含行 + 决策时间线）",
)
async def getChaseDraft(
    chaseDraftId: str,
    response: Response,
    _user: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseDraftDetailRead:
    _noStore(response)
    detail = await _service.getDraft(session, chaseDraftId)
    return _detailRead(detail)


@router.post(
    "/drafts/{chaseDraftId}/validate",
    response_model=ChaseDraftDetailRead,
    summary="校验催货单（→ PENDING_APPROVAL | NEEDS_CORRECTION）",
)
async def validateChaseDraft(
    chaseDraftId: str,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseDraftDetailRead:
    draft = await _service.validateDraft(session, chaseDraftId, actor=admin.userId)
    await session.commit()
    return _detailRead(await _service.getDraft(session, str(draft.chase_draft_id)))


@router.post(
    "/drafts/{chaseDraftId}/approve",
    response_model=ChaseDraftDetailRead,
    summary="审批通过（PENDING_APPROVAL → READY_TO_SEND）",
)
async def approveChaseDraft(
    chaseDraftId: str,
    payload: ChaseDecisionBody,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseDraftDetailRead:
    draft = await _service.approveDraft(
        session, chaseDraftId, actor=admin.userId, note=payload.note
    )
    await session.commit()
    return _detailRead(await _service.getDraft(session, str(draft.chase_draft_id)))


@router.post(
    "/drafts/{chaseDraftId}/reject",
    response_model=ChaseDraftDetailRead,
    summary="审批驳回（PENDING_APPROVAL → REJECTED）",
)
async def rejectChaseDraft(
    chaseDraftId: str,
    payload: ChaseDecisionBody,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseDraftDetailRead:
    draft = await _service.rejectDraft(
        session, chaseDraftId, actor=admin.userId, note=payload.note
    )
    await session.commit()
    return _detailRead(await _service.getDraft(session, str(draft.chase_draft_id)))


@router.post(
    "/drafts/{chaseDraftId}/cancel",
    response_model=ChaseDraftDetailRead,
    summary="取消（活跃态 → CANCELLED）",
)
async def cancelChaseDraft(
    chaseDraftId: str,
    payload: ChaseDecisionBody,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseDraftDetailRead:
    draft = await _service.cancelDraft(
        session, chaseDraftId, actor=admin.userId, note=payload.note
    )
    await session.commit()
    return _detailRead(await _service.getDraft(session, str(draft.chase_draft_id)))


@router.post(
    "/drafts/{chaseDraftId}/send",
    response_model=ChaseDraftDetailRead,
    summary="发送催货单（READY_TO_SEND → SENDING → SENT；本期终态）",
)
async def sendChaseDraft(
    chaseDraftId: str,
    admin: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> ChaseDraftDetailRead:
    draft = await _service.sendDraft(session, chaseDraftId, actor=admin.userId)
    await session.commit()
    return _detailRead(await _service.getDraft(session, str(draft.chase_draft_id)))
