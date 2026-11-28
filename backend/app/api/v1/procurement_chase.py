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
  GET  /drafts/{id}/export        单张下载（xlsx 明细表 / docx 催货函）

权限：admin（对外动作）；限流走 `rateLimitDep`；动作写 audit_log（service 层同事务
决策留痕已有，这里再记操作审计）。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

import logging
from datetime import UTC
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getDb, requireAdmin
from app.domain.exceptions import DomainError, NotFoundError
from app.models.system_config import SystemConfig
from app.schemas.procurement_chase import (
    ChaseDecisionBody,
    ChaseDecisionRead,
    ChaseDraftDetailRead,
    ChaseDraftRead,
    ChaseFromPendingCreate,
    ChaseFromPendingResultRead,
    ChaseLineRead,
)
from app.services.entity_mapping_names import loadMdmDirectory
from app.services.procurement_chase_export_service import (
    COMPANY_NAME_CONFIG_KEY,
    buildChaseExportPayload,
    renderChaseDocx,
    renderChaseExcel,
)
from app.services.procurement_chase_lookup import fetchPendingLinesEnriched
from app.services.procurement_chase_service import (
    ProcurementChaseService,
    defaultReplyDeadline,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["procurement-chase"])

_service = ProcurementChaseService()

#: 导出支持的格式（单张下载，不做批量）
_EXPORT_FORMATS: frozenset[str] = frozenset({"xlsx", "docx"})
#: 导出文件的 ASCII 回退名（RFC 5987 的 `filename` 参数只能是 ASCII）
_EXPORT_ASCII_PREFIX = "chase-"
#: xlsx / docx 的 MIME 类型
_MEDIA_TYPE_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_MEDIA_TYPE_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


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


# ---------------------------------------------------------------------------
# 导出（单张下载）
# ---------------------------------------------------------------------------


def _normalizedDraftId(chaseDraftId: str) -> str | None:
    """催货单号规范化；非法 UUID ⇒ None（⇒ 404，**不是** 500）。

    为什么要这一步：``chase_draft_id`` 是 UUID 列，把任意字符串直接丢给
    PG 会抛 ``ValueError``（⇒ 500「服务端错误」）。而语义上「单号不存在」
    就是 404 —— 与 spec 的错误表一致。
    """
    try:
        return str(UUID(chaseDraftId))
    except ValueError:
        return None


async def _companyNameOf(session: AsyncSession) -> str | None:
    """读系统配置里的公司法定名称（催货函抬头）。

    **未配置 / 值为空 / 读失败 ⇒ None 且记 warning，不阻塞导出** ——
    写死一个「看起来像公司名」的默认值比留空更危险（抬头错误的催货函更糟）。

    读配置用 **savepoint**（``begin_nested``）：配置表读失败时 PG 会把当前
    事务置为 aborted，若不隔离，后面的查询会连环失败 ⇒ 一次配置读取故障
    就能把整张导出打成 500。

    ⚠️ **只有 docx 需要抬头** ⇒ xlsx 路径**不调**本函数（Excel 的信息 Sheet
    没有公司名行，在那儿读配置只会产出「抬头留空」的噪声 warning）。
    """
    try:
        async with session.begin_nested():
            row = await session.execute(
                select(SystemConfig.value).where(
                    SystemConfig.key == COMPANY_NAME_CONFIG_KEY
                )
            )
            raw = row.scalar_one_or_none()
    except Exception:  # noqa: BLE001 - 读不到不影响导出
        logger.warning(
            "读取系统配置 %s 失败，催货函抬头留空",
            COMPANY_NAME_CONFIG_KEY,
            exc_info=True,
        )
        return None
    value = (raw or "").strip()
    if not value:
        logger.warning(
            "系统配置 %s 未配置（或值为空），催货函抬头留空",
            COMPANY_NAME_CONFIG_KEY,
        )
        return None
    return value


async def _supplierNameOf(session: AsyncSession, supplierCode: str) -> str | None:
    """MDM 目录查供应商全名；查不到 ⇒ None（渲染层回落编码，不阻塞导出）。"""
    try:
        async with session.begin_nested():
            directory = await loadMdmDirectory(
                session, entityType="SUPPLIER", codes=(supplierCode,)
            )
    except Exception:  # noqa: BLE001 - 查不到不影响导出
        logger.warning(
            "查询供应商名称失败，回落编码 %s", supplierCode, exc_info=True
        )
        return None
    name = (directory.get(supplierCode) or {}).get("name")
    return str(name) if name else None


def _contentDisposition(filename: str, asciiName: str) -> str:
    """RFC 5987 文件名头：ASCII 回退名 + ``filename*=UTF-8''`` 百分号编码。"""
    return (
        f'attachment; filename="{asciiName}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.get(
    "/drafts/{chaseDraftId}/export",
    summary="导出催货单（Excel 明细表 / Word 催货函）",
)
async def exportChaseDraft(
    chaseDraftId: str,
    format: str = Query(default="xlsx", description="导出格式：xlsx | docx"),
    _user: CurrentUser = Depends(requireAdmin),
    session: AsyncSession = Depends(getDb),
) -> Response:
    """导出**单张**催货单：Excel 明细表 或 Word 催货函（任何状态可下载）。

    权限与其它催货端点同口径（requireAdmin）—— 导出内容与它们同源
    （供应商 / 订单号 / 欠交量 / 逾期天数），不做第二套权限体系。
    """
    if format not in _EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"format 只支持 {sorted(_EXPORT_FORMATS)}，收到 {format!r}",
        )
    draftId = _normalizedDraftId(chaseDraftId)
    if draftId is None:
        # 与「催货单不存在」走**同一个异常类型** ⇒ 两条 404 的响应体形状一致
        # （统一错误包络，而不是 HTTPException 的 {"detail": ...}）
        raise NotFoundError(f"催货单不存在: {chaseDraftId}")
    detail = await _service.getDraft(session, draftId)
    draft = detail["draft"]
    # 只有 Word 催货函有抬头 ⇒ xlsx 不读配置（避免「抬头留空」的噪声 warning）
    companyName = await _companyNameOf(session) if format == "docx" else None
    supplierName = await _supplierNameOf(session, str(draft.supplier_code))
    payload = buildChaseExportPayload(
        draft=draft,
        lines=detail["lines"],
        decisions=detail["decisions"],
        supplierName=supplierName,
        companyName=companyName,
    )
    if format == "xlsx":
        blob = renderChaseExcel(payload)
        mediaType = _MEDIA_TYPE_XLSX
        extension = "xlsx"
    else:
        blob = renderChaseDocx(payload)
        mediaType = _MEDIA_TYPE_DOCX
        extension = "docx"
    stem = payload.chaseDraftId[:8]
    filename = f"催货单_{stem}.{extension}"
    asciiName = f"{_EXPORT_ASCII_PREFIX}{stem}.{extension}"
    response = Response(
        content=blob,
        media_type=mediaType,
        headers={"Content-Disposition": _contentDisposition(filename, asciiName)},
    )
    # 催货函含采购敏感数据 ⇒ 不许缓存（与本文件其它端点同一处理）
    _noStore(response)
    return response


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
