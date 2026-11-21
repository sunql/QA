"""M5-3 询价草稿 API（设计稿 §8.5 的 DRAFT-only 半边）。

路由语义（权限 = 登录 + 服务层校验；DRAFT_WRITE 角色矩阵 = M5-4）：
- `POST /drafts`            创建（**Idempotency-Key 头必填**：同 key 同 payload ⇒
  幂等重放返回既有；异 payload ⇒ 409——DoD 3）
- `GET  /drafts`            我的草稿列表（created_by 过滤）
- `GET  /drafts/{id}`       详情（含决策链 = DoD 4 审计）
- `POST /drafts/{id}/validate`          校验（两步迁移：通过驻留 VALIDATING）
- `POST /drafts/{id}/submit-approval`   提交审批（**必须 VALIDATING**；自批禁止在 service）
- `POST /drafts/{id}/approve` / `/reject` 审批决策（DoD 4 留痕）
- `POST /drafts/{id}/cancel`            取消

**D-4（DRAFT-only）**：无发送端点（SENDING/SENT 在 M5 不可达——状态机负向钉死）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.schemas.procurement_rfq import (
    RfqDecisionBody,
    RfqDecisionRead,
    RfqDraftCreate,
    RfqDraftRead,
    RfqExpectedVersionBody,
)
from app.services.procurement_rfq_service import (
    ProcurementRfqService,
)

router = APIRouter(tags=["procurement-rfqs"])


def _toRead(draft, decisions) -> RfqDraftRead:
    payload = draft.payload_json or {}
    return RfqDraftRead(
        rfqDraftId=draft.rfq_draft_id,
        version=draft.version,
        status=draft.status,
        requestId=draft.request_id,
        deadline=draft.deadline,
        notes=draft.notes,
        supplierKeys=payload.get("supplierKeys") or [],
        lineItems=payload.get("lineItems") or [],
        createdBy=draft.created_by,
        decisions=[
            RfqDecisionRead(
                decision=x.decision, actor=x.actor, note=x.note,
                decidedTime=x.decided_time,
            )
            for x in decisions
        ],
    )


def _payloadFromCreate(body: RfqDraftCreate) -> dict:
    return {
        "supplierKeys": body.supplierKeys,
        "lineItems": [item.model_dump(by_alias=True) for item in body.lineItems],
    }


@router.post("/drafts", response_model=RfqDraftRead, status_code=status.HTTP_201_CREATED)
async def createRfqDraft(
    response: Response,
    body: RfqDraftCreate,
    idempotencyKey: str = Header(alias="Idempotency-Key"),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: ProcurementRfqService = Depends(lambda: ProcurementRfqService()),
) -> RfqDraftRead:
    """创建询价草稿（幂等；201 新建 / 200 重放）。"""
    response.headers["Cache-Control"] = "no-store"
    payload = _payloadFromCreate(body)
    draft, created = await service.createDraftWithStatus(
        session,
        request_id=body.requestId,
        deadline=body.deadline,
        payload=payload,
        notes=body.notes,
        idempotencyKey=idempotencyKey,
        actor=currentUser.userId,
    )
    # 幂等语义：201 = 本次新建；200 = 重放（返回既有草稿）
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    decisions = await service.listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)


@router.get("/drafts", response_model=list[RfqDraftRead])
async def listRfqDrafts(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[RfqDraftRead]:
    response.headers["Cache-Control"] = "no-store"
    drafts = await ProcurementRfqService().listDrafts(
        session, createdBy=currentUser.userId, limit=limit
    )
    return [_toRead(d, []) for d in drafts]


@router.get("/drafts/{rfqDraftId}", response_model=RfqDraftRead)
async def getRfqDraft(
    response: Response,
    rfqDraftId: str,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> RfqDraftRead:
    response.headers["Cache-Control"] = "no-store"
    svc = ProcurementRfqService()
    draft = await svc.getDraft(session, rfqDraftId)
    decisions = await svc.listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)


@router.post("/drafts/{rfqDraftId}/validate", response_model=RfqDraftRead)
async def validateRfqDraft(
    response: Response,
    rfqDraftId: str,
    body: RfqExpectedVersionBody | None = None,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> RfqDraftRead:
    response.headers["Cache-Control"] = "no-store"
    draft = await ProcurementRfqService().validateDraft(
        session, rfqDraftId, actor=currentUser.userId,
        expectedVersion=body.expectedVersion if body else None,
    )
    decisions = await ProcurementRfqService().listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)


@router.post("/drafts/{rfqDraftId}/submit-approval", response_model=RfqDraftRead)
async def submitRfqApproval(
    response: Response,
    rfqDraftId: str,
    body: RfqExpectedVersionBody | None = None,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> RfqDraftRead:
    """提交审批（DoD 2 前置：必须已 validate；自批禁止在决策层）。"""
    response.headers["Cache-Control"] = "no-store"
    draft = await ProcurementRfqService().submitForApproval(
        session, rfqDraftId, actor=currentUser.userId
    )
    decisions = await ProcurementRfqService().listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)


@router.post("/drafts/{rfqDraftId}/approve", response_model=RfqDraftRead)
async def approveRfqDraft(
    response: Response,
    rfqDraftId: str,
    body: RfqDecisionBody | None = None,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> RfqDraftRead:
    response.headers["Cache-Control"] = "no-store"
    draft = await ProcurementRfqService().decideApproval(
        session, rfqDraftId, decision="APPROVE",
        actor=currentUser.userId,
        note=body.note if body else None,
    )
    decisions = await ProcurementRfqService().listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)


@router.post("/drafts/{rfqDraftId}/reject", response_model=RfqDraftRead)
async def rejectRfqDraft(
    response: Response,
    rfqDraftId: str,
    body: RfqDecisionBody | None = None,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> RfqDraftRead:
    response.headers["Cache-Control"] = "no-store"
    draft = await ProcurementRfqService().decideApproval(
        session, rfqDraftId, decision="REJECT",
        actor=currentUser.userId,
        note=body.note if body else None,
    )
    decisions = await ProcurementRfqService().listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)


@router.post("/drafts/{rfqDraftId}/cancel", response_model=RfqDraftRead)
async def cancelRfqDraft(
    response: Response,
    rfqDraftId: str,
    body: RfqExpectedVersionBody | None = None,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> RfqDraftRead:
    response.headers["Cache-Control"] = "no-store"
    draft = await ProcurementRfqService().cancelDraft(
        session, rfqDraftId, actor=currentUser.userId,
        expectedVersion=body.expectedVersion if body else None,
    )
    decisions = await ProcurementRfqService().listDecisions(session, draft.rfq_draft_id)
    return _toRead(draft, decisions)
