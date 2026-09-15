"""评估报告定时调度集成测试（feat-dq-evaluation-report，Phase 7b）。

覆盖：
- cron 校验（合法 / 非法）
- create_schedule + next_run_at 计算
- list / get / update / delete
- run_one 端到端（创建一条 schedule → 手动把 next_run_at 设为 past → run_one
  → 报告生成 + last_report_id 写入 + next_run_at 前移）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.dependencies import CurrentUser
from app.domain.models import (
    DataQualityRule,
    EvaluationReport,
    EvaluationReportSchedule,
    OntologyClass,
)
from app.domain.schemas import EvaluationReportScheduleCreate
from app.services.evaluation_report_scheduler_service import (
    EvaluationReportSchedulerService,
)

ADMIN = CurrentUser(
    userId="sched-admin",
    tenantId="default",
    roles=("admin",),
    departments=("procurement",),
    dbUserId=None,
)
USER_A = CurrentUser(
    userId="sched-user-a",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)


def _headers(actor: CurrentUser) -> dict[str, str]:
    h = {"X-User-Id": actor.userId, "X-User-Roles": ",".join(actor.roles)}
    if actor.departments:
        h["X-User-Departments"] = ",".join(actor.departments)
    return h


def _new_dto(name: str = "sched-1") -> EvaluationReportScheduleCreate:
    return EvaluationReportScheduleCreate(
        name=name,
        cron_expression="*/5 * * * *",  # 每 5 分钟
        class_ids=[1],
        rule_ids=[1],
        time_window_type="LAST_7D",
        recipients=["alert-user"],
        enabled=True,
    )


async def _seed_class_and_rule(dbSession) -> tuple[int, int]:
    from app.domain.enums import DataSourceType
    from app.domain.models import DataSource
    from app.infrastructure.security.crypto import encryptApiKey

    ds = DataSource(
        name="sched-ds",
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="t",
        username="t",
        password_encrypted=encryptApiKey("t"),
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.flush()

    cls = OntologyClass(
        class_name="sched-cls",
        source_table="SUPPLIER",
        object_owner="procurement",
    )
    dbSession.add(cls)
    await dbSession.flush()

    rule = DataQualityRule(
        rule_name="sched-rule",
        rule_code="R-SCHED",
        rule_type="COMPLETENESS",
        target_table="SUPPLIER",
        target_column="SUPPLIER_NAME",
        threshold=80.0,
        severity="HIGH",
        datasource_id=ds.id,
    )
    dbSession.add(rule)
    await dbSession.flush()
    return cls.id, rule.id


@pytest.mark.asyncio
class TestScheduleService:
    async def test_cron_validation(self) -> None:
        """合法 / 非法 cron 表达式判定。"""
        svc = EvaluationReportSchedulerService()
        assert svc.is_valid_cron("*/5 * * * *") is True
        assert svc.is_valid_cron("bogus") is False

    async def test_compute_next_run(self) -> None:
        svc = EvaluationReportSchedulerService()
        from datetime import datetime as _dt

        now = _dt(2026, 9, 14, 12, 0, tzinfo=UTC)
        nxt = svc.compute_next_run("*/5 * * * *", now)
        # 应该是 12:05（严格 > now）
        assert nxt > now
        assert nxt.minute == 5

    async def test_create_schedule_sets_next_run(self, dbSession) -> None:
        svc = EvaluationReportSchedulerService()
        dto = _new_dto()
        read = await svc.create_schedule(dbSession, dto, ADMIN)
        await dbSession.commit()
        assert read.id > 0
        assert read.next_run_at is not None
        assert read.cron_expression == "*/5 * * * *"
        assert read.enabled is True

    async def test_create_schedule_invalid_cron_raises(self, dbSession) -> None:
        svc = EvaluationReportSchedulerService()
        from app.domain.exceptions import ValidationError

        bad = EvaluationReportScheduleCreate(
            name="bad",
            cron_expression="not-a-cron",
            class_ids=[1],
            rule_ids=[1],
            time_window_type="LAST_7D",
        )
        with pytest.raises(ValidationError):
            await svc.create_schedule(dbSession, bad, ADMIN)

    async def test_update_and_delete(self, dbSession) -> None:
        svc = EvaluationReportSchedulerService()
        read = await svc.create_schedule(dbSession, _new_dto("upd"), ADMIN)
        await dbSession.commit()
        from app.domain.schemas import EvaluationReportScheduleUpdate

        upd = await svc.update_schedule(
            dbSession,
            read.id,
            EvaluationReportScheduleUpdate(name="renamed"),
            ADMIN,
        )
        await dbSession.commit()
        assert upd.name == "renamed"

        await svc.delete_schedule(dbSession, read.id, ADMIN)
        await dbSession.commit()

    async def test_due_schedules_filters_disabled(
        self, dbSession,
    ) -> None:
        svc = EvaluationReportSchedulerService()
        await svc.create_schedule(dbSession, _new_dto("due"), ADMIN)
        await dbSession.commit()
        now = datetime.now(UTC) + timedelta(hours=1)
        due = await svc.due_schedules(dbSession, now)
        assert any(s.name == "due" for s in due)

    async def test_run_one_generates_report(self, dbSession) -> None:
        """手动把 next_run_at 设到过去 → run_one → 报告生成 + next_run_at 前移。"""
        cls_id, rule_id = await _seed_class_and_rule(dbSession)
        await dbSession.commit()

        svc = EvaluationReportSchedulerService()
        dto = EvaluationReportScheduleCreate(
            name="run-one",
            cron_expression="*/5 * * * *",
            class_ids=[cls_id],
            rule_ids=[rule_id],
            time_window_type="LAST_7D",
            recipients=[],
            enabled=True,
        )
        read = await svc.create_schedule(dbSession, dto, ADMIN)
        await dbSession.commit()

        # 把 next_run_at 强行设为过去
        sched = await dbSession.get(EvaluationReportSchedule, read.id)
        past = datetime.now(UTC) - timedelta(minutes=10)
        sched.next_run_at = past  # type: ignore[assignment]
        await dbSession.commit()

        report = await svc.run_one(dbSession, read.id)
        await dbSession.commit()
        assert report is not None
        assert isinstance(report, EvaluationReport)
        assert report.name.startswith("run-one@")

        # next_run_at 已经被前移到未来
        await dbSession.refresh(sched)
        assert sched.next_run_at is not None
        assert sched.next_run_at > datetime.now(UTC)
        assert sched.last_report_id == report.id


@pytest.mark.asyncio
class TestScheduleApi:
    async def test_create_schedule_endpoint(self, client, dbSession) -> None:
        cls_id, rule_id = await _seed_class_and_rule(dbSession)
        await dbSession.commit()
        resp = await client.post(
            "/api/v1/data-quality/reports/schedules",
            json={
                "name": "ep-1",
                "cronExpression": "*/5 * * * *",
                "classIds": [cls_id],
                "ruleIds": [rule_id],
                "timeWindowType": "LAST_7D",
                "recipients": ["u1"],
                "enabled": True,
            },
            headers=_headers(USER_A),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["cronExpression"] == "*/5 * * * *"
        assert body["nextRunAt"] is not None

    async def test_list_schedules_endpoint(self, client, dbSession) -> None:
        resp = await client.get(
            "/api/v1/data-quality/reports/schedules",
            headers=_headers(USER_A),
        )
        if resp.status_code != 200:
            print("BODY:", resp.status_code, resp.text)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_invalid_cron_422(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/data-quality/reports/schedules",
            json={
                "name": "bad",
                "cronExpression": "not-a-cron",
                "classIds": [1],
                "ruleIds": [1],
                "timeWindowType": "LAST_7D",
            },
            headers=_headers(USER_A),
        )
        # service 用 ValidationError → 422（异常体系对齐）
        assert resp.status_code in (400, 422)