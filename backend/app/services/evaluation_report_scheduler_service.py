"""评估报告定时调度 service（feat-dq-evaluation-report，Phase 7b）。

参考 ``agent_scheduler_service``：croniter 解析、claim-AND-update 防并发双跑、
``runSchedule`` 失败不抛异常（next_run_at 已前移，下次重试）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import ReportStatus, ReportTimeWindowType
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import EvaluationReport, EvaluationReportSchedule
from app.domain.schemas import (
    EvaluationReportCreate,
    EvaluationReportScheduleCreate,
    EvaluationReportScheduleRead,
    EvaluationReportScheduleUpdate,
)
from app.services.acl_service import AclService
from app.services.evaluation_report_service import EvaluationReportService

logger = logging.getLogger(__name__)


_WINDOW_OFFSETS: dict[ReportTimeWindowType, timedelta] = {
    ReportTimeWindowType.LAST_7D: timedelta(days=7),
    ReportTimeWindowType.LAST_30D: timedelta(days=30),
    # LAST_RUN：从上一次 last_run_at 到 now；首次跑取 7 天兜底
}


def _resolve_window(
    window_type: ReportTimeWindowType,
    last_run_at: datetime | None,
) -> tuple[datetime, datetime]:
    """根据 window_type 算本次评估的 [start, end]。end = now()，start 按 type 倒推。"""
    end = datetime.now(UTC)
    if window_type == ReportTimeWindowType.LAST_RUN and last_run_at is not None:
        start = last_run_at
    elif window_type == ReportTimeWindowType.LAST_RUN:
        start = end - _WINDOW_OFFSETS[ReportTimeWindowType.LAST_7D]
    else:
        start = end - _WINDOW_OFFSETS.get(
            window_type, _WINDOW_OFFSETS[ReportTimeWindowType.LAST_7D]
        )
    return start, end


class EvaluationReportSchedulerService:
    """评估报告定时 CRUD + worker 编排。"""

    def __init__(
        self,
        *,
        report_service: EvaluationReportService | None = None,
        acl: AclService | None = None,
    ) -> None:
        self._reports = report_service or EvaluationReportService()
        self._acl = acl or AclService()

    # ------------------------------------------------------------------
    # cron 解析
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_cron(expression: str) -> bool:
        return bool(expression) and croniter.is_valid(expression)

    @staticmethod
    def compute_next_run(expression: str, from_time: datetime) -> datetime:
        if not EvaluationReportSchedulerService.is_valid_cron(expression):
            raise ValidationError(
                message=f"invalid cron expression: {expression!r}"
            )
        return croniter(expression, from_time).get_next(datetime)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def list_schedules(
        self, session: AsyncSession, *, include_disabled: bool = True,
    ) -> list[EvaluationReportScheduleRead]:
        stmt = select(EvaluationReportSchedule).order_by(
            EvaluationReportSchedule.id.desc()
        )
        if not include_disabled:
            stmt = stmt.where(EvaluationReportSchedule.enabled.is_(True))
        rows = (await session.execute(stmt)).scalars().all()
        return [
            EvaluationReportScheduleRead.model_validate(r) for r in rows
        ]

    async def get_schedule(
        self, session: AsyncSession, schedule_id: int,
    ) -> EvaluationReportSchedule:
        row = await session.get(EvaluationReportSchedule, schedule_id)
        if row is None:
            raise NotFoundError(message=f"schedule {schedule_id} not found")
        return row

    async def create_schedule(
        self,
        session: AsyncSession,
        dto: EvaluationReportScheduleCreate,
        actor: CurrentUser,
    ) -> EvaluationReportScheduleRead:
        """创建 schedule：校验 cron + 落库 + 计算 next_run_at。"""
        if not self.is_valid_cron(dto.cron_expression):
            raise ValidationError(
                message=f"invalid cron expression: {dto.cron_expression!r}"
            )
        now = datetime.now(UTC)
        next_run_at = self.compute_next_run(dto.cron_expression, now)

        row = EvaluationReportSchedule(
            name=dto.name,
            cron_expression=dto.cron_expression,
            class_ids=list(dto.class_ids),
            rule_ids=list(dto.rule_ids),
            time_window_type=dto.time_window_type,
            recipients=list(dto.recipients),
            enabled=dto.enabled,
            next_run_at=next_run_at,
            owner=(actor.departments[0] if actor.departments else None),
            created_by=actor.userId,
        )
        session.add(row)
        await session.flush()
        return EvaluationReportScheduleRead.model_validate(row)

    async def update_schedule(
        self,
        session: AsyncSession,
        schedule_id: int,
        dto: EvaluationReportScheduleUpdate,
        actor: CurrentUser,
    ) -> EvaluationReportScheduleRead:
        row = await self.get_schedule(session, schedule_id)
        self._acl.assertCanModify(
            user=actor,
            entity_owner=row.owner,
            entity_label="EvaluationReportSchedule",
            entity_code=str(schedule_id),
        )

        if dto.name is not None:
            row.name = dto.name
        if dto.recipients is not None:
            row.recipients = list(dto.recipients)
        if dto.enabled is not None:
            row.enabled = dto.enabled
            if dto.enabled and (
                row.next_run_at is None or row.next_run_at <= datetime.now(UTC)
            ):
                row.next_run_at = self.compute_next_run(
                    row.cron_expression, datetime.now(UTC)
                )
        if dto.cron_expression is not None:
            if not self.is_valid_cron(dto.cron_expression):
                raise ValidationError(
                    message=f"invalid cron expression: {dto.cron_expression!r}"
                )
            row.cron_expression = dto.cron_expression
            row.next_run_at = self.compute_next_run(
                row.cron_expression, datetime.now(UTC)
            )
        await session.flush()
        return EvaluationReportScheduleRead.model_validate(row)

    async def delete_schedule(
        self,
        session: AsyncSession,
        schedule_id: int,
        actor: CurrentUser,
    ) -> None:
        row = await self.get_schedule(session, schedule_id)
        self._acl.assertCanModify(
            user=actor,
            entity_owner=row.owner,
            entity_label="EvaluationReportSchedule",
            entity_code=str(schedule_id),
        )
        # 没有 deleted_at 列；用 enabled=False 语义化「已删除」
        row.enabled = False
        row.next_run_at = None
        await session.flush()

    # ------------------------------------------------------------------
    # Worker 入口
    # ------------------------------------------------------------------

    async def due_schedules(
        self, session: AsyncSession, now: datetime,
    ) -> list[EvaluationReportSchedule]:
        """enabled 且 next_run_at <= now 的到期 schedule（按 id 升序取前 50）。"""
        rows = (
            await session.execute(
                select(EvaluationReportSchedule)
                .where(
                    EvaluationReportSchedule.enabled.is_(True),
                    EvaluationReportSchedule.next_run_at.is_not(None),
                    EvaluationReportSchedule.next_run_at <= now,
                )
                .order_by(EvaluationReportSchedule.next_run_at)
                .limit(50)
            )
        ).scalars().all()
        return list(rows)

    async def run_one(
        self,
        session: AsyncSession,
        schedule_id: int,
        now: datetime | None = None,
    ) -> EvaluationReport | None:
        """claim-AND-update：前移 next_run_at 防并发双跑，再调
        ``EvaluationReportService.createReport`` 生成报告 + 写 last_report_id。

        失败不抛异常：next_run_at 已前移，下次还会重试；本次返回 None。
        """
        now = now or datetime.now(UTC)
        schedule = await session.get(EvaluationReportSchedule, schedule_id)
        if (
            schedule is None
            or not schedule.enabled
            or schedule.next_run_at is None
            or schedule.next_run_at > now
        ):
            return None

        old_next = schedule.next_run_at
        # late worker：从 now 算以保证 future
        from_time = old_next if old_next > now else now
        new_next = self.compute_next_run(schedule.cron_expression, from_time)

        # claim：条件 UPDATE
        result = await session.execute(
            EvaluationReportSchedule.__table__.update()
            .where(
                EvaluationReportSchedule.id == schedule_id,
                EvaluationReportSchedule.next_run_at == old_next,
                EvaluationReportSchedule.enabled.is_(True),
            )
            .values(next_run_at=new_next)
        )
        if result.rowcount != 1:
            return None  # 并发 worker 抢占或已停用

        try:
            start, end = _resolve_window(
                schedule.time_window_type, schedule.last_run_at,
            )
            dto = EvaluationReportCreate(
                name=f"{schedule.name}@{start:%Y%m%d%H%M}",
                description=f"Auto-generated by schedule {schedule.id}",
                class_ids=list(schedule.class_ids or []),
                rule_ids=list(schedule.rule_ids or []),
                time_window_start=start,
                time_window_end=end,
                tags=["scheduler"],
                status=ReportStatus.PUBLISHED.value,
            )
            actor = CurrentUser(
                userId=f"scheduler:{schedule.id}",
                tenantId="default",
                roles=("user",),
                departments=(schedule.owner,) if schedule.owner else (),
                dbUserId=None,
            )
            report = await self._reports.createReport(session, dto, actor)
            schedule.last_run_at = now
            schedule.last_report_id = report.id
            # 收件人通知（Phase 7c）：如服务可用则调用；不可用则 no-op
            try:
                from app.services.in_app_message_service import (
                    InAppMessageService,
                )

                msg_svc = InAppMessageService()
                for uid in schedule.recipients or []:
                    await msg_svc.create_message(
                        recipient_user_id=uid,
                        title="评估报告已生成",
                        body=(
                            f"报告 {report.name} 已生成。"
                            f"窗口：{start:%Y-%m-%d} → {end:%Y-%m-%d}"
                        ),
                        link_url=f"/data-quality/reports/{report.id}",
                    )
            except ImportError:
                # Phase 7c 尚未交付；占位不抛
                pass
            await session.commit()
            return report
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scheduler run failed schedule_id=%s err=%s",
                schedule_id,
                exc,
            )
            # next_run_at 已前移，下次重试
            await session.commit()
            return None


def get_evaluation_report_scheduler_service() -> (
    EvaluationReportSchedulerService
):
    return EvaluationReportSchedulerService()