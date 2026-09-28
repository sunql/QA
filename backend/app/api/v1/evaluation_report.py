"""数据质量评估报告 REST API（feat-dq-evaluation-report，Phase 3-7）。

挂在 /api/v1/data-quality/reports：
  GET    /api/v1/data-quality/reports               列表（name/class/rule/createdBy/date 过滤，分页）
  POST   /api/v1/data-quality/reports               创建（201）
  GET    /api/v1/data-quality/reports/{id}          详情（含 snapshot）
  PATCH  /api/v1/data-quality/reports/{id}          更新元数据（仅 name/desc/tags/status，ACL）
  DELETE /api/v1/data-quality/reports/{id}          软删（ACL）
  POST   /api/v1/data-quality/reports/{id}/regenerate  重算 snapshot（ACL）
  GET    /api/v1/data-quality/reports/{id}/samples  违规样本
  GET    /api/v1/data-quality/reports/{id}/export?format=pdf|excel  导出文件流（Phase 7a）

Phase 8 还会加：
  GET    /api/v1/data-quality/reports/{id}/compare?otherId=...
  POST   /api/v1/data-quality/reports/{id}/share
  DELETE /api/v1/data-quality/reports/shares/{share_id}
  GET    /api/v1/data-quality/reports/share/{token}     token-only 公开访问
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    EvaluationReportCompareRead,
    EvaluationReportCreate,
    EvaluationReportListRead,
    EvaluationReportProgress,
    EvaluationReportRead,
    EvaluationReportScheduleCreate,
    EvaluationReportScheduleRead,
    EvaluationReportScheduleUpdate,
    EvaluationReportShareCreate,
    EvaluationReportShareRead,
    EvaluationReportUpdate,
    ViolationSampleRead,
)
from app.services.data_quality_violation_sample_service import (
    DataQualityViolationSampleService,
    get_data_quality_violation_sample_service,
)
from app.services.evaluation_report_compare_service import (
    EvaluationReportCompareService,
)
from app.services.evaluation_report_export_service import (
    EvaluationReportExportService,
    get_evaluation_report_export_service,
)
from app.services.evaluation_report_scheduler_service import (
    EvaluationReportSchedulerService,
    get_evaluation_report_scheduler_service,
)
from app.services.evaluation_report_service import (
    EvaluationReportService,
    _reportToRead,
    get_evaluation_report_service,
)
from app.services.evaluation_report_share_service import (
    EvaluationReportShareService,
    get_evaluation_report_share_service,
)

router = APIRouter()


@router.get("", response_model=EvaluationReportListRead)
async def listEvaluationReports(
    _user: CurrentUser = Depends(getCurrentUser),
    name: str | None = Query(default=None, max_length=200),
    classId: int | None = Query(default=None, ge=1, alias="classId"),
    ruleId: int | None = Query(default=None, ge=1, alias="ruleId"),
    createdBy: str | None = Query(default=None, max_length=50, alias="createdBy"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportListRead:
    return await service.listReports(
        session,
        name=name,
        class_id=classId,
        rule_id=ruleId,
        created_by=createdBy,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=EvaluationReportRead, status_code=status.HTTP_201_CREATED)
async def createEvaluationReport(
    payload: EvaluationReportCreate,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportRead:
    """创建报告 + 调度后台快照任务（feat-dq-evaluation-report-progress）。

    返回 status=PENDING + progress=空骨架。前端轮询 GET /reports/{id}/progress
    拿进度直到 status=COMPLETED 或 FAILED。
    """
    report = await service.createReport(session, payload, user)
    # 后台跑 snapshot；service.runSnapshotJob 内部 fallback 构造 dispatcher/sample_service
    background_tasks.add_task(service.runSnapshotJob, report.id)
    # 再次查询，让 progress Pydantic 字段被序列化进响应（model_validate 也能拿到）
    refreshed = await service.getReport(session, report.id)
    return _reportToRead(refreshed)


# Phase 9：报告评估进度轮询（feat-dq-evaluation-report-progress，2026-09-15）。
# 必须在 /{report_id} 之前注册，否则会被路径参数吞掉返回 422。
@router.get(
    "/{report_id}/progress",
    response_model=EvaluationReportProgress,
)
async def getEvaluationReportProgress(
    report_id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportProgress:
    """取报告当前评估进度（前端每 1s 轮询直到 stage ∈ {COMPLETED, FAILED}）。"""
    return await service.getProgress(session, report_id)


# ---------------------------------------------------------------------------
# Phase 7b：定时 schedule CRUD（必须放在 /{report_id} 之前，否则会被吞掉）
# ---------------------------------------------------------------------------


@router.post(
    "/schedules",
    response_model=EvaluationReportScheduleRead,
    status_code=status.HTTP_201_CREATED,
)
async def createEvaluationReportSchedule(
    payload: EvaluationReportScheduleCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    scheduler: EvaluationReportSchedulerService = Depends(
        get_evaluation_report_scheduler_service,
    ),
) -> EvaluationReportScheduleRead:
    return await scheduler.create_schedule(session, payload, user)


@router.get(
    "/schedules",
    response_model=list[EvaluationReportScheduleRead],
)
async def listEvaluationReportSchedules(
    _user: CurrentUser = Depends(getCurrentUser),
    include_disabled: bool = Query(default=True),
    session: AsyncSession = Depends(getDb),
    scheduler: EvaluationReportSchedulerService = Depends(
        get_evaluation_report_scheduler_service,
    ),
) -> list[EvaluationReportScheduleRead]:
    return await scheduler.list_schedules(
        session, include_disabled=include_disabled,
    )


@router.patch(
    "/schedules/{schedule_id}",
    response_model=EvaluationReportScheduleRead,
)
async def updateEvaluationReportSchedule(
    schedule_id: int,
    payload: EvaluationReportScheduleUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    scheduler: EvaluationReportSchedulerService = Depends(
        get_evaluation_report_scheduler_service,
    ),
) -> EvaluationReportScheduleRead:
    return await scheduler.update_schedule(
        session, schedule_id, payload, user,
    )


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deleteEvaluationReportSchedule(
    schedule_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    scheduler: EvaluationReportSchedulerService = Depends(
        get_evaluation_report_scheduler_service,
    ),
) -> None:
    await scheduler.delete_schedule(session, schedule_id, user)
    from fastapi import Response as _Resp

    return _Resp(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{report_id}", response_model=EvaluationReportRead)
async def getEvaluationReport(
    report_id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportRead:
    report = await service.getReport(session, report_id)
    return _reportToRead(report)


@router.patch("/{report_id}", response_model=EvaluationReportRead)
async def updateEvaluationReport(
    report_id: int,
    payload: EvaluationReportUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportRead:
    report = await service.updateReport(session, report_id, payload, user)
    return _reportToRead(report)


@router.delete("/{report_id}", response_model=EvaluationReportRead)
async def softDeleteEvaluationReport(
    report_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportRead:
    report = await service.softDeleteReport(session, report_id, user)
    return _reportToRead(report)


@router.post("/{report_id}/regenerate", response_model=EvaluationReportRead)
async def regenerateEvaluationReportSnapshot(
    report_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportRead:
    report = await service.regenerateSnapshot(session, report_id, user)
    return _reportToRead(report)


@router.get("/{report_id}/samples", response_model=list[ViolationSampleRead])
async def listEvaluationReportSamples(
    report_id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    ruleId: int | None = Query(default=None, ge=1, alias="ruleId"),
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
    sample_service: DataQualityViolationSampleService = Depends(
        get_data_quality_violation_sample_service,
    ),
) -> list[ViolationSampleRead]:
    # 详情接口先确认报告存在并有权限读取（任何人可读，ACL 在 ACL=read-only=all）
    await service.getReport(session, report_id)
    rows = await sample_service.listForReport(
        session, report_id=report_id, rule_id=ruleId, limit=limit,
    )
    return [ViolationSampleRead.model_validate(r) for r in rows]


@router.get(
    "/{report_id}/export",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
            },
        },
    },
)
async def exportEvaluationReport(
    report_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    format: str = Query(default="pdf", pattern="^(pdf|excel)$", alias="format"),
    include_samples: bool = Query(default=True),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
    export_service: EvaluationReportExportService = Depends(
        get_evaluation_report_export_service,
    ),
) -> Response:
    """导出报告为 PDF 或 Excel 文件流（ACL = owner/admin）。

    实现：调用 ``EvaluationReportExportService`` 取 ORM 拼 payload → 调相应
    builder 拿字节 → 返回 ``Response``。文件名按 format 选扩展名。
    """
    # ACL：owner 或 admin 才能导出（与 regenerate/delete 对齐）
    report = await service.getReport(session, report_id)
    from app.services.acl_service import AclService

    AclService().assertCanModify(
        user=user,
        entity_owner=report.owner,
        entity_label="EvaluationReport",
        entity_code=str(report.id),
    )
    if format == "pdf":
        content = await export_service.export_pdf(
            session, report_id, include_samples=include_samples,
        )
        media_type = "application/pdf"
        ext = "pdf"
    else:
        content = await export_service.export_excel(
            session, report_id, include_samples=include_samples,
        )
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        ext = "xlsx"
    filename = f"evaluation-report-{report_id}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# Phase 8a：报告对比（任意登录用户可读）
@router.get(
    "/{report_id}/compare",
    response_model=EvaluationReportCompareRead,
)
async def compareEvaluationReports(
    report_id: int,
    other_id: int = Query(..., alias="otherId"),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: EvaluationReportService = Depends(get_evaluation_report_service),
) -> EvaluationReportCompareRead:
    """对比两份报告。任一登录用户可用（仅读 snapshot，不暴露 owner 隐私）。"""
    left = await service.getReport(session, report_id)
    right = await service.getReport(session, other_id)
    return EvaluationReportCompareService().compare_by_ids(
        left_id=report_id,
        right_id=other_id,
        left_snapshot=left.snapshot,
        right_snapshot=right.snapshot,
    )


# Phase 8b：分享链接
@router.post(
    "/{report_id}/share",
    response_model=EvaluationReportShareRead,
    status_code=status.HTTP_201_CREATED,
)
async def createShare(
    report_id: int,
    payload: EvaluationReportShareCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    share_service: EvaluationReportShareService = Depends(
        get_evaluation_report_share_service,
    ),
) -> EvaluationReportShareRead:
    """创建分享 token。ACL = report.owner/admin。"""
    return await share_service.create_share(
        session,
        report_id=report_id,
        expires_in_days=payload.expires_in_days,
        actor=user,
    )


@router.get(
    "/{report_id}/share",
    response_model=list[EvaluationReportShareRead],
)
async def listShares(
    report_id: int,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    share_service: EvaluationReportShareService = Depends(
        get_evaluation_report_share_service,
    ),
) -> list[EvaluationReportShareRead]:
    """列出某报告的现存（未过期）share。"""
    return await share_service.list_shares(session, report_id=report_id)


@router.delete(
    "/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revokeShare(
    share_id: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    share_service: EvaluationReportShareService = Depends(
        get_evaluation_report_share_service,
    ),
) -> None:
    """撤销分享（物理删 share 行）。ACL = 报告 owner/admin。"""
    await share_service.revoke_share(session, share_id=share_id, actor=user)


@router.get(
    "/share/{token}",
    response_model=EvaluationReportRead,
)
async def resolveShare(
    token: str,
    session: AsyncSession = Depends(getDb),
    share_service: EvaluationReportShareService = Depends(
        get_evaluation_report_share_service,
    ),
) -> EvaluationReportRead:
    """按 token 公开访问（不需登录）。access_count 自增。"""
    return await share_service.resolve_share_to_read(session, token=token)
