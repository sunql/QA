"""Agent 批量调度（Phase 7 G5）集成测试（真实 PG + 完整 API 链路）。

覆盖：
- POST /agents/{code}/schedules → 201 + AgentScheduleRead（cron 校验 + next_run_at 计算）
- POST 无效 cron → 422
- GET /agents/{code}/schedules → 列表
- PATCH .../toggle → is_active 翻转
- DELETE ... → 删除后 404
- 安全：非 admin 非 owner 写操作 → 403；owner 与 admin 可写
- 调度执行（service.runSchedule）→ agent_run_log 落库
  （status/tokens/cost/actor=scheduler:{id}）+ last_run_at 更新 + next_run_at 前移
- 失败调度（DRAFT agent）→ run_log status=error 优雅记录，不崩溃
- claim 幂等：next_run_at 前移后同一 schedule 不再重复执行
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import (
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
)
from app.domain.models import AgentRunLog, AgentSchedule
from app.domain.schemas import AgentDefinitionCreate, AgentScheduleCreate
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_runtime_service import AGENT_TOOLS
from app.services.agent_scheduler_service import AgentSchedulerService
from app.tests.integration.test_agent_runtime_api import (
    _defaultPolicies,
    _seedFeatureAndValue,
    _seedFeatureDatasource,
    _seedSupplier,
)

AUTH_ADMIN = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
AUTH_OWNER = {"X-User-Id": "bob", "X-User-Roles": "analyst", "X-User-Departments": "procurement"}
AUTH_OTHER = {"X-User-Id": "alice", "X-User-Roles": "analyst", "X-User-Departments": "finance"}
_ADMIN = CurrentUser(userId="test-admin", roles=("admin",))
_OWNER = CurrentUser(userId="bob", roles=("analyst",), departments=("procurement",))


async def _seedAgent(dbSession: AsyncSession, code: str, *, status: AgentStatus = AgentStatus.ACTIVE) -> None:
    """用 AgentRegistryService 注册一个 Agent，owner=procurement（非 admin 部门）。

    复用 test_agent_runtime_api 的显式分层策略（按工具 data_object + data_layers）。
    """
    svc = AgentRegistryService()
    dto = AgentDefinitionCreate(
        agent_code=code,
        agent_name=f"{code} G5 集成测试",
        description="Phase 7 G5 批量调度",
        trigger_type=AgentTriggerType.SCHEDULED,
        response_latency=AgentResponseLatency.BATCH,
        data_domains=["PROCUREMENT"],
        data_layers=["FEATURE"],
        status=status,
        version="v1.0",
        policies=_defaultPolicies(code),
    )
    await svc.createAgent(dbSession, dto, _OWNER)


def _schedulePayload(expression: str = "0 9 * * *", input_text: str = "供应商 100001 风险等级") -> dict:
    return {"cron_expression": expression, "params": {"input": input_text}}


class TestAgentScheduleApi:
    async def test_create_schedule_computes_next_run_at(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        resp = await client.post(
            "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["agentCode"] == "SUPPLIER_RISK_AGENT"
        assert body["cronExpression"] == "0 9 * * *"
        assert body["isActive"] is True
        assert body["nextRunAt"] is not None
        assert body["params"] == {"input": "供应商 100001 风险等级"}

    async def test_create_schedule_invalid_cron_422(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        resp = await client.post(
            "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
            json=_schedulePayload(expression="0 99 * * *"),
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 422, resp.text

    async def test_create_schedule_unknown_agent_404(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/agents/UNKNOWN_AGENT/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 404, resp.text

    async def test_list_schedules(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        for _ in range(2):
            resp = await client.post(
                "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
                json=_schedulePayload(),
                headers=AUTH_ADMIN,
            )
            assert resp.status_code == 201, resp.text
        resp = await client.get(
            "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules", headers=AUTH_ADMIN
        )
        assert resp.status_code == 200
        items = resp.json()
        assert isinstance(items, list) and len(items) == 2
        assert all(i["cronExpression"] == "0 9 * * *" for i in items)

    async def test_toggle_schedule(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        created = (
            await client.post(
                "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
                json=_schedulePayload(),
                headers=AUTH_ADMIN,
            )
        ).json()
        sid = created["id"]
        resp = await client.patch(
            f"/api/v1/agents/SUPPLIER_RISK_AGENT/schedules/{sid}/toggle",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["isActive"] is False
        # 再切回
        resp = await client.patch(
            f"/api/v1/agents/SUPPLIER_RISK_AGENT/schedules/{sid}/toggle",
            headers=AUTH_ADMIN,
        )
        assert resp.json()["isActive"] is True

    async def test_delete_schedule(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        sid = (
            await client.post(
                "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
                json=_schedulePayload(),
                headers=AUTH_ADMIN,
            )
        ).json()["id"]
        resp = await client.delete(
            f"/api/v1/agents/SUPPLIER_RISK_AGENT/schedules/{sid}", headers=AUTH_ADMIN
        )
        assert resp.status_code == 204, resp.text
        resp = await client.get(
            "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules", headers=AUTH_ADMIN
        )
        assert resp.json() == []


class TestAgentScheduleAuthz:
    async def test_non_owner_non_admin_cannot_create(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        resp = await client.post(
            "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
            json=_schedulePayload(),
            headers=AUTH_OTHER,
        )
        assert resp.status_code == 403, resp.text

    async def test_owner_can_create(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        resp = await client.post(
            "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
            json=_schedulePayload(),
            headers=AUTH_OWNER,
        )
        assert resp.status_code == 201, resp.text

    async def test_non_owner_cannot_toggle_or_delete(self, client, dbSession) -> None:
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        sid = (
            await client.post(
                "/api/v1/agents/SUPPLIER_RISK_AGENT/schedules",
                json=_schedulePayload(),
                headers=AUTH_ADMIN,
            )
        ).json()["id"]
        assert (
            await client.patch(
                f"/api/v1/agents/SUPPLIER_RISK_AGENT/schedules/{sid}/toggle",
                headers=AUTH_OTHER,
            )
        ).status_code == 403
        assert (
            await client.delete(
                f"/api/v1/agents/SUPPLIER_RISK_AGENT/schedules/{sid}", headers=AUTH_OTHER
            )
        ).status_code == 403


class TestAgentScheduleDispatch:
    async def test_dispatch_runs_and_records_log(self, dbSession) -> None:
        """到期 schedule 执行成功：run_log 落库（actor=scheduler:{id}），
        last_run_at 更新，next_run_at 前移。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        await _seedSupplier(dbSession, 100001, "SUP000001")
        await _seedFeatureDatasource(dbSession)
        await _seedFeatureAndValue(dbSession, 5001, "late_payment_rate", "SUP000001", 0.35)

        svc = AgentSchedulerService()
        created = await svc.createSchedule(
            dbSession,
            "SUPPLIER_RISK_AGENT",
            AgentScheduleCreate(cron_expression="0 9 * * *", params={"input": "供应商 100001 风险等级"}),
            _ADMIN,
        )
        sid = created.id
        # 拨到过去使其到期
        row = (
            await dbSession.execute(select(AgentSchedule).where(AgentSchedule.id == sid))
        ).scalar_one()
        row.next_run_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await dbSession.commit()

        log = await svc.runSchedule(dbSession, row, datetime.now(timezone.utc))
        assert log is not None
        assert log.status == "success"
        assert log.agent_code == "SUPPLIER_RISK_AGENT"
        assert log.actor == f"scheduler:{sid}"
        assert log.tokens_used >= 0
        assert log.cost >= 0

        # 调度行推进：last_run_at 已置，next_run_at 移到未来
        fresh = (
            await dbSession.execute(select(AgentSchedule).where(AgentSchedule.id == sid))
        ).scalar_one()
        assert fresh.last_run_at is not None
        assert fresh.next_run_at > datetime.now(timezone.utc)
        # 不再到期 → 再次 runSchedule 直接 None（不重复执行）
        assert await svc.runSchedule(dbSession, fresh, datetime.now(timezone.utc)) is None

    async def test_dispatch_draft_agent_records_error_log(self, dbSession) -> None:
        """DRAFT 不可运行：run_log status=error 优雅记录，不抛异常。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT", status=AgentStatus.DRAFT)
        svc = AgentSchedulerService()
        created = await svc.createSchedule(
            dbSession,
            "SUPPLIER_RISK_AGENT",
            AgentScheduleCreate(cron_expression="0 9 * * *", params={"input": "供应商 100001 风险等级"}),
            _ADMIN,
        )
        sid = created.id
        row = (
            await dbSession.execute(select(AgentSchedule).where(AgentSchedule.id == sid))
        ).scalar_one()
        row.next_run_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await dbSession.commit()

        log = await svc.runSchedule(dbSession, row, datetime.now(timezone.utc))
        assert log is not None
        assert log.status == "error"
        assert log.error is not None

    async def test_due_schedules_only_active_and_past(self, dbSession) -> None:
        """dueSchedules：仅 is_active 且 next_run_at <= now 的 schedule。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        svc = AgentSchedulerService()
        now = datetime.now(timezone.utc)
        past = datetime(2026, 1, 1, tzinfo=timezone.utc)
        future_ts = now + timedelta(hours=1)

        active_due = await svc.createSchedule(
            dbSession, "SUPPLIER_RISK_AGENT",
            AgentScheduleCreate(cron_expression="0 9 * * *", params={"input": "供应商 100001 风险等级"}),
            _ADMIN,
        )
        future = await svc.createSchedule(
            dbSession, "SUPPLIER_RISK_AGENT",
            AgentScheduleCreate(cron_expression="0 9 * * *", params={"input": "供应商 100001 风险等级"}),
            _ADMIN,
        )
        inactive = await svc.createSchedule(
            dbSession, "SUPPLIER_RISK_AGENT",
            AgentScheduleCreate(cron_expression="0 9 * * *", params={"input": "供应商 100001 风险等级"}),
            _ADMIN,
        )
        await svc.toggleSchedule(dbSession, "SUPPLIER_RISK_AGENT", inactive.id, _ADMIN)

        # 拨动：active_due → 过去；future → 未来 1h；inactive → 过去但停用
        for sid, ts in ((active_due.id, past), (future.id, future_ts), (inactive.id, past)):
            row = (
                await dbSession.execute(select(AgentSchedule).where(AgentSchedule.id == sid))
            ).scalar_one()
            row.next_run_at = ts
            await dbSession.commit()

        due = await svc.dueSchedules(dbSession, now)
        due_ids = {s.id for s in due}
        assert active_due.id in due_ids
        assert future.id not in due_ids
        assert inactive.id not in due_ids

    async def test_run_log_history_queryable(self, dbSession) -> None:
        """run_log 可查询（前端 schedule 面板历史视图的数据源）。"""
        await _seedAgent(dbSession, "SUPPLIER_RISK_AGENT")
        await _seedSupplier(dbSession, 100001, "SUP000001")
        await _seedFeatureDatasource(dbSession)
        await _seedFeatureAndValue(dbSession, 5002, "late_payment_rate", "SUP000001", 0.35)
        svc = AgentSchedulerService()
        created = await svc.createSchedule(
            dbSession, "SUPPLIER_RISK_AGENT",
            AgentScheduleCreate(cron_expression="0 9 * * *", params={"input": "供应商 100001 风险等级"}),
            _ADMIN,
        )
        row = (
            await dbSession.execute(select(AgentSchedule).where(AgentSchedule.id == created.id))
        ).scalar_one()
        row.next_run_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await dbSession.commit()
        await svc.runSchedule(dbSession, row, datetime.now(timezone.utc))

        logs = await svc.listRunLogs(dbSession, "SUPPLIER_RISK_AGENT", _ADMIN, schedule_id=created.id)
        assert len(logs) == 1
        assert logs[0].status == "success"
        assert logs[0].actor == f"scheduler:{created.id}"
