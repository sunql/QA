"""M4 Report 模板 API（A8）。

GET  /api/v1/reports/templates                # 模板列表（模板选择器）
POST /api/v1/reports/generate                 # 同步生成（PENDING_REVIEW）
GET  /api/v1/reports                          # 分页列表（状态过滤，非 admin 收敛可见性）
GET  /api/v1/reports/{instanceId}             # 详情（可见性守卫：不可见 → 404）
POST /api/v1/reports/{instanceId}/review      # 审批（admin only，对齐 audit_history）

actor 一律从 getCurrentUser 依赖派生，不读任何客户端自报字段（ACL memory 教训）。
可见性规则（蓝图 §21 硬约束 2）：
- admin：全见
- 非 admin：自己的（任意状态）+ 全员 APPROVED
- 不可见 → 404（不把「不存在」与「无权看」区分开，避免存在性侧信道；
  选择口径见 service.getReportVisible docstring）
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.models import ReportInstance
from app.domain.report_schemas import (
    ReportGenerateRequest,
    ReportInstanceRead,
    ReportListPage,
    ReportListItemRead,
    ReportReviewRequest,
    ReportTemplateOut,
)
from app.services.model_config_service import ModelConfigService
from app.services.report_template_service import (
    REPORT_STATUSES,
    ReportTemplateService,
)

router = APIRouter(prefix="/reports", tags=["reports"])

# 模块级单例：集成测试 monkeypatch _service._llmFactory 注入 fake（test_chat_api 同模式）
_service = ReportTemplateService()


@router.get(
    "/templates",
    response_model=list[ReportTemplateOut],
    status_code=status.HTTP_200_OK,
    summary="报告模板列表（供前端模板选择器）",
)
async def listTemplates(
    _user: CurrentUser = Depends(getCurrentUser),
) -> list[ReportTemplateOut]:
    metas = _service.listTemplates()
    return [ReportTemplateOut(**meta) for meta in metas]


@router.post(
    "/generate",
    response_model=ReportInstanceRead,
    status_code=status.HTTP_201_CREATED,
    summary="同步生成报告（数据绑定零 LLM + 恰好 ≤1 次总结 LLM，落库即 PENDING_REVIEW）",
)
async def generateReport(
    dto: ReportGenerateRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> ReportInstanceRead:
    configs = await ModelConfigService().list(db, activeOnly=True)
    instance = await _service.generateReport(
        db,
        templateCode=dto.template_code,
        params=dto.params,
        actor=_user,
        configs=configs,
    )
    return ReportInstanceRead.model_validate(instance)


@router.get(
    "",
    response_model=ReportListPage,
    status_code=status.HTTP_200_OK,
    summary="报告分页列表（非 admin 只看自己的 + 全员 APPROVED）",
)
async def listReports(
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="状态过滤：PENDING_REVIEW/APPROVED/REJECTED"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportListPage:
    if status_filter is not None and status_filter not in REPORT_STATUSES:
        # 早期 422（pydantic enum 只挡请求体；查询参数手工校验 + 枚举提示）
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"非法状态过滤值: {status_filter}（可选：{', '.join(REPORT_STATUSES)}）",
        )
    rows, total = await _service.listReports(
        db, viewer=_user, status=status_filter, limit=limit, offset=offset
    )
    return ReportListPage(
        rows=[ReportListItemRead.model_validate(r) for r in rows],
        total=total,
    )


@router.get(
    "/{instanceId}",
    response_model=ReportInstanceRead,
    status_code=status.HTTP_200_OK,
    summary="报告详情（可见性守卫：不可见 → 404）",
)
async def getReport(
    instanceId: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> ReportInstanceRead:
    report = await _service.getReportVisible(db, instanceId, _user)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在")
    return ReportInstanceRead.model_validate(report)


@router.post(
    "/{instanceId}/review",
    response_model=ReportInstanceRead,
    status_code=status.HTTP_200_OK,
    summary="报告审批（admin only，对齐 audit_history ACL 模式）",
)
async def reviewReport(
    instanceId: int,
    dto: ReportReviewRequest,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
) -> ReportInstanceRead:
    from app.services.messages_zh import MSG_REPORT_NOT_FOUND

    report = await db.get(ReportInstance, instanceId)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=MSG_REPORT_NOT_FOUND
        )
    updated = await _service.reviewReport(
        db, report, decision=dto.decision, note=dto.note, reviewer=_admin
    )
    return ReportInstanceRead.model_validate(updated)
