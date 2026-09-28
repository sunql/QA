"""数据质量评估报告 service（feat-dq-evaluation-report，Phase 3+4）。

提供 EvaluationReport 的 CRUD + ACL + 审计 + 快照计算 + 样本落库：
- create_report：写入一行 evaluation_report，snapshot 由 compute_snapshot 生成，
  并按规则批量采 top-N 违规样本写入 data_quality_violation_sample
- list_reports：按 name/class_id/rule_id/created_by/date 过滤，分页
- get_report：单行详情（不含 deleted_at IS NOT NULL 的行）
- update_report：仅 name/description/tags/status 可改；ACL = owner 部门 或 admin
- soft_delete_report：设 deleted_at，ACL 同上
- regenerate_snapshot：清旧样本 → 重算 snapshot → 重采样本（同一事务）

ACL 借用 AclService.assertCanModify（user 角色=admin 或 owner ∈ user.departments）。
审计走 AuditService.record，写在同一事务内。
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import ReportStatus, RuleType
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.models import (
    DataQualityRule,
    DataQualityScore,
    DataQualityViolationSample,
    EvaluationReport,
    OntologyClass,
)
from app.domain.schemas import (
    EvaluationReportCreate,
    EvaluationReportListRead,
    EvaluationReportProgress,
    EvaluationReportRead,
    EvaluationReportUpdate,
)
from app.services.acl_service import AclService
from app.services.audit_service import AuditService
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher
from app.services.data_quality_violation_sample_service import (
    DEFAULT_SAMPLE_LIMIT,
    DataQualityViolationSampleService,
)
from app.services.messages_zh import (
    MSG_DQ_EVAL_REPORT_NAME_EXISTS,
    MSG_DQ_EVAL_REPORT_NOT_FOUND,
)

logger = logging.getLogger(__name__)

_audit = AuditService()
_acl = AclService()

SNAPSHOT_SCHEMA_VERSION = 1

# 异步进度（feat-dq-evaluation-report-progress，2026-09-15）。
# progress JSONB 内部键（与 EvaluationReportProgress 字段对齐，snake_case）。
_PROGRESS_STAGE_PENDING = "PENDING"
_PROGRESS_STAGE_RUNNING = "RUNNING"
_PROGRESS_STAGE_COMPLETED = "COMPLETED"
_PROGRESS_STAGE_FAILED = "FAILED"

# 6 维度 key 集合（timeliness 暂为 None）；与 DataQualityScore 列对齐
_DIMENSION_KEYS = (
    "completeness",
    "validity",
    "uniqueness",
    "consistency",
    "timeliness",
    "referential",
)
_RULE_TYPE_TO_DIM = {
    RuleType.COMPLETENESS.value: "completeness",
    RuleType.VALIDITY.value: "validity",
    RuleType.UNIQUENESS.value: "uniqueness",
    RuleType.CONSISTENCY.value: "consistency",
    RuleType.REFERENTIAL.value: "referential",
}


def _reportToRead(report: EvaluationReport) -> EvaluationReportRead:
    return EvaluationReportRead.model_validate(report)


def _progressFromDb(report: EvaluationReport) -> EvaluationReportProgress | None:
    """从 ORM 行的 progress JSONB 反序列化为 Pydantic（缺失则 None）。"""
    raw = getattr(report, "progress", None)
    if not raw:
        return None
    return EvaluationReportProgress.model_validate(raw)


def _reportToDict(report: EvaluationReport) -> dict[str, Any]:
    """ORM → dict 转换（审计用；snake_case 与 DB 列对齐）。"""
    return {
        "id": report.id,
        "name": report.name,
        "description": report.description,
        "class_ids": list(report.class_ids or []),
        "rule_ids": list(report.rule_ids or []),
        "time_window_start": report.time_window_start.isoformat() if report.time_window_start else None,
        "time_window_end": report.time_window_end.isoformat() if report.time_window_end else None,
        "status": report.status.value if hasattr(report.status, "value") else report.status,
        "tags": list(report.tags or []),
        "owner": report.owner,
        "created_by": report.created_by,
        "deleted_at": report.deleted_at.isoformat() if report.deleted_at else None,
    }


async def _validate_class_ids_exist(
    session: AsyncSession,
    class_ids: list[int],
) -> None:
    """校验所有 class_id 都在 ontology_class 表里。"""
    if not class_ids:
        return
    stmt = select(OntologyClass.id).where(OntologyClass.id.in_(class_ids))
    rows = (await session.execute(stmt)).scalars().all()
    found = set(rows)
    missing = [cid for cid in class_ids if cid not in found]
    if missing:
        raise NotFoundError(
            f"本体类 id={missing} 不存在；请先在本体管理中创建"
        )


async def _validate_rule_ids_exist(
    session: AsyncSession,
    rule_ids: list[int],
) -> None:
    if not rule_ids:
        return
    stmt = select(DataQualityRule.id).where(DataQualityRule.id.in_(rule_ids))
    rows = (await session.execute(stmt)).scalars().all()
    found = set(rows)
    missing = [rid for rid in rule_ids if rid not in found]
    if missing:
        raise NotFoundError(
            f"数据质量规则 id={missing} 不存在"
        )


async def _check_name_conflict(
    session: AsyncSession,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """同一 created_by + name 唯一（软删除外）。"""
    stmt = select(EvaluationReport.id).where(
        and_(
            EvaluationReport.name == name,
            EvaluationReport.deleted_at.is_(None),
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(EvaluationReport.id != exclude_id)
    rows = (await session.execute(stmt)).scalars().all()
    if rows:
        raise ConflictError(MSG_DQ_EVAL_REPORT_NAME_EXISTS.format(name=name))


def _buildEmptySnapshot(
    *,
    class_ids: list[int],
    rule_ids: list[int],
) -> dict[str, Any]:
    """空快照骨架（snapshot 内部键保持 snake_case，参见
    [[dq-eval-report-snapshot-snake-case]]）。"""
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "evaluated_at": datetime.now(UTC).isoformat(),
        "evaluation_duration_ms": 0,
        "evaluated_rule_count": len(rule_ids),
        "class_count": len(class_ids),
        "overall": {"score": None, "status": "PENDING"},
        "dimensions": {k: None for k in _DIMENSION_KEYS},
        "tables": [],
        "trend": {"available": False, "window_days": 0, "series": []},
    }


async def _fetchTrendSeries(
    session: AsyncSession,
    *,
    target_tables: list[str],
    window_days: int = 30,
) -> dict[str, Any]:
    """从 data_quality_score 取近 N 天每个 target_table 的 overall_score 时序。

    仅返回 GLOBAL（target_table='*'）那条，便于做整体趋势线。
    """
    if not target_tables:
        return {"available": False, "window_days": window_days, "series": []}
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    stmt = (
        select(
            DataQualityScore.evaluated_at,
            DataQualityScore.overall_score,
        )
        .where(
            DataQualityScore.target_table.in_(target_tables),
            DataQualityScore.evaluated_at >= cutoff,
        )
        .order_by(DataQualityScore.evaluated_at.asc())
    )
    rows = list((await session.execute(stmt)).all())
    if not rows:
        return {"available": False, "window_days": window_days, "series": []}
    series = [
        {
            "date": r.evaluated_at.date().isoformat() if r.evaluated_at else None,
            "overall_score": float(r.overall_score) if r.overall_score is not None else None,
        }
        for r in rows
    ]
    return {"available": True, "window_days": window_days, "series": series}


def _aggregateSnapshot(
    *,
    class_ids: list[int],
    rule_ids: list[int],
    results: list[Any],
    evaluation_duration_ms: int,
    trend: dict[str, Any],
) -> dict[str, Any]:
    """从 evaluator 批量结果 + trend 数据构造完整 snapshot。

    results 是 EvaluationResult 列表（含 status=PASS/FAIL/ERROR）。
    """
    snapshot = _buildEmptySnapshot(class_ids=list(class_ids), rule_ids=list(rule_ids))
    snapshot["evaluation_duration_ms"] = int(evaluation_duration_ms)

    # 1) 按 target_table 分组
    by_table: dict[str, dict[str, Any]] = {}
    dim_totals: dict[str, list[float]] = {k: [] for k in _DIMENSION_KEYS}
    overall_pass_count = 0
    overall_fail_count = 0

    for r in results:
        table_key = (
            f"datasource_{r.datasource_id}"
            if not getattr(r, "target_table", None)
            else getattr(r, "target_table", None)
        )
        bucket = by_table.setdefault(
            table_key,
            {
                "target_table": getattr(r, "target_table", None) or table_key,
                "rules": [],
                "_pass_sum": 0.0,
                "_rule_count": 0,
            },
        )
        # violation_count / pass_rate（缺省字段做容错）
        violation_count = max(
            int(getattr(r, "total_count", 0)) - int(getattr(r, "passed_count", 0)),
            0,
        )
        bucket["rules"].append(
            {
                "rule_id": r.rule_id,
                "rule_code": getattr(r, "rule_code", "") or "",
                # feat-report-rules-zh-name (2026-09-15)：snapshot 加 rule_name + severity；
                # 前端报告「规则明细」表 Rule 列用 rule_name（业务可读）、
                # Severity 列用真实值（之前永远 None）；两者缺省时降级到 fallback 不破表。
                "rule_name": getattr(r, "rule_name", None),
                "severity": getattr(r, "severity", None),
                "rule_type": getattr(r, "rule_type", None).value
                if getattr(r, "rule_type", None) is not None
                else None,
                "total_count": int(getattr(r, "total_count", 0)),
                "passed_count": int(getattr(r, "passed_count", 0)),
                "violation_count": violation_count,
                "pass_rate": float(getattr(r, "pass_rate", 0.0)),
                "status": getattr(r, "status", "ERROR"),
                "evaluated_at": (
                    getattr(r, "evaluated_at", None).isoformat()
                    if getattr(r, "evaluated_at", None) is not None
                    else None
                ),
                "duration_ms": int(getattr(r, "duration_ms", 0)),
                "message": getattr(r, "message", None),
            }
        )
        bucket["_rule_count"] += 1
        if getattr(r, "status", "ERROR") == "PASS":
            bucket["_pass_sum"] += float(getattr(r, "pass_rate", 0.0))
            overall_pass_count += 1
        elif getattr(r, "status", "ERROR") == "FAIL":
            overall_fail_count += 1
        # 维度分：按 rule_type 落到对应维度的 rate
        rt = getattr(r, "rule_type", None)
        rt_key = rt.value if hasattr(rt, "value") else str(rt) if rt else None
        dim_key = _RULE_TYPE_TO_DIM.get(rt_key or "")
        if dim_key and getattr(r, "status", "ERROR") != "ERROR":
            dim_totals[dim_key].append(float(getattr(r, "pass_rate", 0.0)))

    # 2) 每个 table 算 overall_score（按 rule pass_rate 平均）
    tables_out = []
    for _key, bucket in by_table.items():
        rc = bucket["_rule_count"] or 1
        score = round(bucket["_pass_sum"] / rc, 4)
        bucket.pop("_pass_sum", None)
        bucket.pop("_rule_count", None)
        bucket["overall_score"] = score
        tables_out.append(bucket)

    # 3) 6 维度平均
    dimensions_out: dict[str, float | None] = {}
    for k in _DIMENSION_KEYS:
        vals = dim_totals[k]
        dimensions_out[k] = round(sum(vals) / len(vals), 4) if vals else None
    snapshot["dimensions"] = dimensions_out

    # 4) overall
    if tables_out:
        snapshot["tables"] = tables_out
        avg = sum(t["overall_score"] for t in tables_out) / len(tables_out)
        snapshot["overall"] = {
            "score": round(avg, 4),
            "status": "PASS" if avg >= 80.0 else "FAIL",
            "passed_rule_count": overall_pass_count,
            "failed_rule_count": overall_fail_count,
        }
    else:
        snapshot["overall"] = {
            "score": None,
            "status": "PENDING",
            "passed_rule_count": 0,
            "failed_rule_count": 0,
        }

    # 5) trend
    snapshot["trend"] = trend
    return snapshot


async def _computeSnapshot(
    session: AsyncSession,
    *,
    class_ids: list[int],
    rule_ids: list[int],
    time_window_start: datetime,
    time_window_end: datetime,
    dispatcher: DataQualityEvaluatorDispatcher | None = None,
    sample_service: DataQualityViolationSampleService | None = None,
    report_id_for_samples: int | None = None,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """真实 snapshot 计算入口（Phase 4）。

    1. 调 evaluator dispatcher 跑批量评估（带 time_window）
    2. 聚合 results → tables[] + dimensions + overall
    3. 查 DataQualityScore 拿 trend
    4. （可选）report_id 非 None 时调 sample service 写违规样本
    返回 snapshot dict；调用方负责 commit/refresh。
    """
    dispatcher = dispatcher or DataQualityEvaluatorDispatcher()
    time_window = (time_window_start, time_window_end)
    started = time.perf_counter()
    batch = await dispatcher.evaluateBatch(
        session, list(rule_ids), time_window=time_window,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)

    # trend 数据源：rule 的 target_table 去重（缺省时跳过）
    stmt = select(DataQualityRule.target_table).where(
        DataQualityRule.id.in_(rule_ids),
    )
    target_tables = sorted({
        r for (r,) in (await session.execute(stmt)).all() if r
    })
    trend = await _fetchTrendSeries(session, target_tables=target_tables)

    snapshot = _aggregateSnapshot(
        class_ids=class_ids,
        rule_ids=rule_ids,
        results=list(batch.results),
        evaluation_duration_ms=duration_ms,
        trend=trend,
    )

    # 违规采样（create/regenerate 路径调用，report_id 已知）
    if report_id_for_samples is not None and rule_ids:
        sample_service = sample_service or DataQualityViolationSampleService(dispatcher)
        # 先清掉该 report_id 的旧样本（regenerate 场景）
        await session.execute(
            delete(DataQualityViolationSample).where(
                DataQualityViolationSample.report_id == report_id_for_samples,
            ),
        )
        # 把 evaluator 结果按 rule_id 索引，传 violation_count 给 sampler
        # （不再用 -1 占位——让前端「违规样本」表的 total_violations 列显示真实数字）
        results_by_rule = {r.rule_id: r for r in batch.results}
        # 拉 rules
        rules_stmt = select(DataQualityRule).where(DataQualityRule.id.in_(rule_ids))
        rules = list((await session.execute(rules_stmt)).scalars().all())
        for rule in rules:
            eval_result = results_by_rule.get(rule.id)
            if eval_result is None:
                # 规则被 evaluateBatch 跳过（rule_id 不在 results_by_id）——理论上
                # 不会发生，但容错：写 violation_count=0，让前端至少能渲染一行
                violation_count = 0
            else:
                violation_count = max(
                    int(getattr(eval_result, "total_count", 0))
                    - int(getattr(eval_result, "passed_count", 0)),
                    0,
                )
            await sample_service.sampleForRule(
                session,
                report_id=report_id_for_samples,
                rule=rule,
                violation_count=violation_count,
                limit=sample_limit,
                time_window=time_window,
            )
        # 重新统计 violation_count 填进 snapshot.tables[].rules[]
        sample_stmt = select(DataQualityViolationSample).where(
            DataQualityViolationSample.report_id == report_id_for_samples,
        )
        samples_by_rule: dict[int, DataQualityViolationSample] = {
            s.rule_id: s for s in (await session.execute(sample_stmt)).scalars().all()
        }
        for tbl in snapshot.get("tables", []):
            for rule_entry in tbl.get("rules", []):
                sample = samples_by_rule.get(int(rule_entry["rule_id"]))
                if sample is not None:
                    rule_entry["sample_size"] = int(sample.sample_size)
    return snapshot


class EvaluationReportService:
    """评估报告 service。"""

    def __init__(
        self,
        acl: AclService | None = None,
        dispatcher: DataQualityEvaluatorDispatcher | None = None,
        sample_service: DataQualityViolationSampleService | None = None,
    ) -> None:
        self._acl = acl or AclService()
        self._dispatcher = dispatcher
        self._sample_service = sample_service

    # -------------------- list --------------------

    async def listReports(
        self,
        session: AsyncSession,
        *,
        name: str | None = None,
        class_id: int | None = None,
        rule_id: int | None = None,
        created_by: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> EvaluationReportListRead:
        """分页列表；按 created_time DESC 排序。"""
        stmt = select(EvaluationReport).where(EvaluationReport.deleted_at.is_(None))
        count_stmt = (
            select(func.count())
            .select_from(EvaluationReport)
            .where(EvaluationReport.deleted_at.is_(None))
        )
        conditions = []
        if name:
            conditions.append(EvaluationReport.name.ilike(f"%{name}%"))
        if class_id is not None:
            # JSONB 数组 contains：@>
            conditions.append(EvaluationReport.class_ids.contains([class_id]))
        if rule_id is not None:
            conditions.append(EvaluationReport.rule_ids.contains([rule_id]))
        if created_by:
            conditions.append(EvaluationReport.created_by == created_by)
        if start is not None:
            conditions.append(EvaluationReport.created_time >= start)
        if end is not None:
            conditions.append(EvaluationReport.created_time <= end)
        if conditions:
            stmt = stmt.where(and_(*conditions))
            count_stmt = count_stmt.where(and_(*conditions))
        stmt = (
            stmt.order_by(EvaluationReport.created_time.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        return EvaluationReportListRead(
            rows=[_reportToRead(r) for r in rows],
            total=total,
        )

    # -------------------- get --------------------

    async def getReport(
        self,
        session: AsyncSession,
        report_id: int,
    ) -> EvaluationReport:
        report = await session.get(EvaluationReport, report_id)
        if report is None or report.deleted_at is not None:
            raise NotFoundError(MSG_DQ_EVAL_REPORT_NOT_FOUND.format(id=report_id))
        return report

    # -------------------- create --------------------

    async def createReport(
        self,
        session: AsyncSession,
        dto: EvaluationReportCreate,
        actor: CurrentUser,
    ) -> EvaluationReport:
        """创建报告 + 调度后台快照任务（feat-dq-evaluation-report-progress）。

        行为：
        1. 同步阶段：校验 → 写一行 → 设 status=PENDING / progress=空骨架 → commit
        2. 后台任务：由 caller 通过 FastAPI BackgroundTasks 启动 `runSnapshotJob`

        返回的行 status=PENDING，前端据此跳转到 detail 页并轮询 progress。
        """
        await _validate_class_ids_exist(session, dto.class_ids)
        await _validate_rule_ids_exist(session, dto.rule_ids)
        await _check_name_conflict(session, dto.name)

        # 业务可见 status：用户可显式选 DRAFT/PUBLISHED；如果选了 PENDING/RUNNING
        # 等内部状态，按 PENDING 处理（不应通过 API 直接进入运行态）。
        request_status = ReportStatus(dto.status)
        if request_status not in {ReportStatus.DRAFT, ReportStatus.PUBLISHED}:
            request_status = ReportStatus.PENDING

        report = EvaluationReport(
            name=dto.name,
            description=dto.description,
            class_ids=dto.class_ids,
            rule_ids=dto.rule_ids,
            time_window_start=dto.time_window_start,
            time_window_end=dto.time_window_end,
            status=request_status,
            tags=dto.tags,
            snapshot=_buildEmptySnapshot(class_ids=dto.class_ids, rule_ids=dto.rule_ids),
            snapshot_version=SNAPSHOT_SCHEMA_VERSION,
            owner=getattr(actor, "departments", ()) and actor.departments[0] or None,
            created_by=actor.userId,
            progress={
                "stage": _PROGRESS_STAGE_PENDING,
                "completed": 0,
                "total": len(dto.rule_ids),
                "current_rule_id": None,
                "current_rule_code": None,
                "message": None,
                "started_at": None,
                "finished_at": None,
            },
        )
        session.add(report)
        await session.flush()
        await _audit.record(
            session,
            entity_type="evaluation_report",
            entity_id=report.id,
            action="CREATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=None,
            after=_reportToDict(report),
        )
        await session.commit()
        await session.refresh(report)
        return report

    # -------------------- async snapshot job --------------------

    async def runSnapshotJob(
        self,
        report_id: int,
        *,
        dispatcher: DataQualityEvaluatorDispatcher | None = None,
        sample_service: DataQualityViolationSampleService | None = None,
    ) -> None:
        """后台任务：跑快照 + 写样本 + 写进度（feat-dq-evaluation-report-progress）。

        用法：
            background_tasks.add_task(
                service.runSnapshotJob, report.id,
                dispatcher=svc._dispatcher, sample_service=svc._sample_service,
            )

        设计：
        - 每个 DB 写入（status / progress / snapshot）独立 commit，避免长事务。
        - 进度 stage 在关键节点切换：PENDING → RUNNING → COMPLETED/FAILED。
        - 异常被捕获并写入 progress.message + status=FAILED，绝不让进程挂掉。
        """
        from app.infrastructure.database import getSessionFactory

        dispatcher = dispatcher or self._dispatcher or DataQualityEvaluatorDispatcher()
        sample_service = sample_service or self._sample_service or DataQualityViolationSampleService(dispatcher)
        total = 0
        async with getSessionFactory()() as session:
            try:
                # 切到 RUNNING
                report = await session.get(EvaluationReport, report_id)
                if report is None or report.deleted_at is not None:
                    logger.warning("runSnapshotJob: report id=%s 不存在或已删除", report_id)
                    return
                # 仅 PENDING / PUBLISHED 才推进；DRAFT 跳过（草稿暂存）；
                # 已 RUNNING / COMPLETED / FAILED 的重启场景忽略。
                # 修 MDM-test 卡 PENDING：UI 默认 EvaluationReportCreate.status='PUBLISHED'，
                # 原契约只接受 PENDING 导致 BackgroundTasks 启动后立即 return。
                if report.status not in {ReportStatus.PENDING, ReportStatus.PUBLISHED}:
                    logger.info(
                        "runSnapshotJob: report id=%s 当前 status=%s，跳过（不重启）",
                        report_id, report.status,
                    )
                    return
                rule_ids = list(report.rule_ids or [])
                total = len(rule_ids)
                started_at = datetime.now(UTC).isoformat()
                report.status = ReportStatus.RUNNING
                report.progress = {
                    "stage": _PROGRESS_STAGE_RUNNING,
                    "completed": 0,
                    "total": total,
                    "current_rule_id": None,
                    "current_rule_code": None,
                    "message": "评估已开始",
                    "started_at": started_at,
                    "finished_at": None,
                }
                await session.commit()

                # 拉 rule 元数据（按顺序给出 current_rule_code 用于 UI 提示）
                rule_codes: dict[int, str] = {}
                if rule_ids:
                    rows = (await session.execute(
                        select(DataQualityRule.id, DataQualityRule.rule_code)
                        .where(DataQualityRule.id.in_(rule_ids))
                    )).all()
                    rule_codes = {rid: code for rid, code in rows}

                # 跑快照（内部 dispatcher 逐条跑，已经支持 time_window）
                snapshot = await _computeSnapshot(
                    session,
                    class_ids=list(report.class_ids or []),
                    rule_ids=rule_ids,
                    time_window_start=report.time_window_start,
                    time_window_end=report.time_window_end,
                    dispatcher=dispatcher,
                    sample_service=sample_service,
                    report_id_for_samples=report.id,
                )
                # 写 snapshot + 切 COMPLETED
                report = await session.get(EvaluationReport, report_id)
                if report is None:
                    return
                report.snapshot = snapshot
                report.status = ReportStatus.COMPLETED
                # 进度：completed 兜底按 results 数填（snapshot.tables[].rules[] 平展）
                evaluated_count = sum(
                    len(t.get("rules", []) or [])
                    for t in (snapshot.get("tables") or [])
                )
                report.progress = {
                    "stage": _PROGRESS_STAGE_COMPLETED,
                    "completed": evaluated_count or total,
                    "total": total,
                    "current_rule_id": None,
                    "current_rule_code": None,
                    "message": "评估已完成",
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat(),
                }
                await _audit.record(
                    session,
                    entity_type="evaluation_report",
                    entity_id=report.id,
                    action="UPDATE",
                    actor=report.created_by or "system",
                    actor_departments=None,
                    before=_reportToDict(report),
                    after=_reportToDict(report),
                )
                await session.commit()
            except Exception as exc:  # noqa: BLE001 — 兜底写入失败状态
                logger.exception("runSnapshotJob: report id=%s 评估异常", report_id)
                try:
                    report = await session.get(EvaluationReport, report_id)
                    if report is not None:
                        report.status = ReportStatus.FAILED
                        existing = dict(report.progress or {})
                        existing.update({
                            "stage": _PROGRESS_STAGE_FAILED,
                            "message": str(exc)[:500] or exc.__class__.__name__,
                            "finished_at": datetime.now(UTC).isoformat(),
                        })
                        report.progress = existing
                        await session.commit()
                except Exception:  # noqa: BLE001
                    logger.exception("runSnapshotJob: 写 FAILED 状态失败，放弃")

    # -------------------- progress --------------------

    async def getProgress(
        self,
        session: AsyncSession,
        report_id: int,
    ) -> EvaluationReportProgress:
        """取报告当前进度（feat-dq-evaluation-report-progress）。"""
        report = await self.getReport(session, report_id)
        return _progressFromDb(report) or EvaluationReportProgress(stage=_PROGRESS_STAGE_PENDING)

    # -------------------- update --------------------

    async def updateReport(
        self,
        session: AsyncSession,
        report_id: int,
        dto: EvaluationReportUpdate,
        actor: CurrentUser,
    ) -> EvaluationReport:
        report = await self.getReport(session, report_id)
        self._acl.assertCanModify(
            actor,
            report.owner,
            "EvaluationReport",
            str(report.id),
        )
        before = _reportToDict(report)
        if dto.name is not None and dto.name != report.name:
            await _check_name_conflict(session, dto.name, exclude_id=report.id)
            report.name = dto.name
        if dto.description is not None:
            report.description = dto.description
        if dto.tags is not None:
            report.tags = list(dto.tags)
        if dto.status is not None:
            report.status = ReportStatus(dto.status)
        await session.flush()
        await _audit.record(
            session,
            entity_type="evaluation_report",
            entity_id=report.id,
            action="UPDATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before,
            after=_reportToDict(report),
        )
        await session.commit()
        await session.refresh(report)
        return report

    # -------------------- soft delete --------------------

    async def softDeleteReport(
        self,
        session: AsyncSession,
        report_id: int,
        actor: CurrentUser,
    ) -> EvaluationReport:
        report = await self.getReport(session, report_id)
        self._acl.assertCanModify(
            actor,
            report.owner,
            "EvaluationReport",
            str(report.id),
        )
        before = _reportToDict(report)
        report.deleted_at = datetime.now(UTC)
        await session.flush()
        await _audit.record(
            session,
            entity_type="evaluation_report",
            entity_id=report.id,
            action="DELETE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before,
            after=_reportToDict(report),
        )
        await session.commit()
        await session.refresh(report)
        return report

    # -------------------- regenerate snapshot --------------------

    async def regenerateSnapshot(
        self,
        session: AsyncSession,
        report_id: int,
        actor: CurrentUser,
    ) -> EvaluationReport:
        report = await self.getReport(session, report_id)
        self._acl.assertCanModify(
            actor,
            report.owner,
            "EvaluationReport",
            str(report.id),
        )
        before = _reportToDict(report)
        new_snapshot = await _computeSnapshot(
            session,
            class_ids=list(report.class_ids or []),
            rule_ids=list(report.rule_ids or []),
            time_window_start=report.time_window_start,
            time_window_end=report.time_window_end,
            dispatcher=self._dispatcher,
            sample_service=self._sample_service,
            report_id_for_samples=report.id,
        )
        report.snapshot = new_snapshot
        report.snapshot_version = SNAPSHOT_SCHEMA_VERSION
        await session.flush()
        await _audit.record(
            session,
            entity_type="evaluation_report",
            entity_id=report.id,
            action="UPDATE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before,
            after=_reportToDict(report),
        )
        await session.commit()
        await session.refresh(report)
        return report


def get_evaluation_report_service() -> EvaluationReportService:
    """FastAPI Depends 工厂。"""
    return EvaluationReportService()
