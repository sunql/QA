"""agent_scheduler_service audit writes — 6 cases (CREATE/UPDATE/DELETE x basic + actor_departments + before+after)."""

from __future__ import annotations

import pytest
from fastapi import status

from app.dependencies import CurrentUser
from app.domain.enums import AgentResponseLatency, AgentStatus, AgentTriggerType
from app.domain.models import AgentSchedule
from app.domain.schemas import AgentDefinitionCreate, AgentScheduleCreate
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_scheduler_service import AgentSchedulerService
from app.services.agent_tools import AGENT_DEFAULT_BINDINGS

AUTH_ADMIN = {"X-User-Id": "audit-sched-admin", "X-User-Roles": "admin"}
_OWNER = CurrentUser(userId="bob", roles=("analyst",), departments=("procurement",))


async def _seedAgent(dbSession, code: str) -> None:
    """Register an Agent owned by procurement (non-admin department)."""
    svc = AgentRegistryService()
    dto = AgentDefinitionCreate(
        agent_code=code,
        agent_name=f"{code} Audit Test",
        description="audit test agent",
        trigger_type=AgentTriggerType.SCHEDULED,
        response_latency=AgentResponseLatency.BATCH,
        data_domains=["PROCUREMENT"],
        data_layers=["FEATURE"],
        status=AgentStatus.ACTIVE,
        version="v1.0",
        policies=[],
    )
    await svc.createAgent(dbSession, dto, _OWNER)


def _schedulePayload(expression: str = "0 9 * * *", input_text: str = "供应商 100001 风险等级") -> dict:
    return {"cron_expression": expression, "params": {"input": input_text}}


@pytest.mark.asyncio
class TestAgentSchedulerAudit:
    """6 cases for agent_scheduler_service audit writes."""

    # ------------------------------------------------------------------
    # 1. CREATE — audit log written with CREATE action
    # ------------------------------------------------------------------

    async def test_create_schedule_writes_audit(self, client, dbSession) -> None:
        """POST /agents/{code}/schedules → audit record with action=CREATE."""
        await _seedAgent(dbSession, "AUDIT_SCHED_CREATE")
        resp = await client.post(
            "/api/v1/agents/AUDIT_SCHED_CREATE/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text
        schedule_id = resp.json()["id"]

        # Drain outbox so audit worker processes the enqueued event
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_schedule&action=CREATE",
            headers=AUTH_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        assert any(
            r["entityId"] == schedule_id and r["action"] == "CREATE"
            for r in rows
        ), f"CREATE audit not found for schedule {schedule_id}"

    # ------------------------------------------------------------------
    # 2. UPDATE (toggle) — audit log written with UPDATE action
    # ------------------------------------------------------------------

    async def test_update_schedule_writes_audit(self, client, dbSession) -> None:
        """PATCH .../toggle → audit record with action=UPDATE."""
        await _seedAgent(dbSession, "AUDIT_SCHED_UPDATE")
        create_resp = await client.post(
            "/api/v1/agents/AUDIT_SCHED_UPDATE/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        schedule_id = create_resp.json()["id"]

        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)

        toggle_resp = await client.patch(
            f"/api/v1/agents/AUDIT_SCHED_UPDATE/schedules/{schedule_id}/toggle",
            headers=AUTH_ADMIN,
        )
        assert toggle_resp.status_code == status.HTTP_200_OK, toggle_resp.text

        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_schedule&action=UPDATE",
            headers=AUTH_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        assert any(
            r["entityId"] == schedule_id and r["action"] == "UPDATE"
            for r in rows
        ), f"UPDATE audit not found for schedule {schedule_id}"

    # ------------------------------------------------------------------
    # 3. DELETE — audit log written with DELETE action
    # ------------------------------------------------------------------

    async def test_delete_schedule_writes_audit(self, client, dbSession) -> None:
        """DELETE ... → audit record with action=DELETE."""
        await _seedAgent(dbSession, "AUDIT_SCHED_DELETE")
        create_resp = await client.post(
            "/api/v1/agents/AUDIT_SCHED_DELETE/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        schedule_id = create_resp.json()["id"]

        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)

        del_resp = await client.delete(
            f"/api/v1/agents/AUDIT_SCHED_DELETE/schedules/{schedule_id}",
            headers=AUTH_ADMIN,
        )
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT, del_resp.text

        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_schedule&action=DELETE",
            headers=AUTH_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        assert any(
            r["entityId"] == schedule_id and r["action"] == "DELETE"
            for r in rows
        ), f"DELETE audit not found for schedule {schedule_id}"

    # ------------------------------------------------------------------
    # 4. CREATE — actor_departments injected from header
    # ------------------------------------------------------------------

    async def test_create_schedule_actor_departments_injected(self, client, dbSession) -> None:
        """CREATE audit captures X-User-Departments header as actorDepartments."""
        await _seedAgent(dbSession, "AUDIT_SCHED_DEPT")
        resp = await client.post(
            "/api/v1/agents/AUDIT_SCHED_DEPT/schedules",
            json=_schedulePayload(),
            headers={**AUTH_ADMIN, "X-User-Departments": "procurement,finance"},
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.text

        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_schedule",
            headers=AUTH_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r for r in rows
            if r["action"] == "CREATE"
            and "procurement" in (r.get("actorDepartments") or "")
        ]
        assert len(matching) >= 1, "actorDepartments not captured in CREATE audit"

    # ------------------------------------------------------------------
    # 5. UPDATE (toggle) — beforeJson and afterJson both present
    # ------------------------------------------------------------------

    async def test_update_schedule_has_before_and_after(self, client, dbSession) -> None:
        """UPDATE audit record has both beforeJson and afterJson."""
        await _seedAgent(dbSession, "AUDIT_SCHED_BEFORE")
        create_resp = await client.post(
            "/api/v1/agents/AUDIT_SCHED_BEFORE/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        schedule_id = create_resp.json()["id"]

        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)

        await client.patch(
            f"/api/v1/agents/AUDIT_SCHED_BEFORE/schedules/{schedule_id}/toggle",
            headers=AUTH_ADMIN,
        )
        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=agent_schedule&action=UPDATE&entity_id={schedule_id}",
            headers=AUTH_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1, "No UPDATE audit found"
        assert any(
            r.get("beforeJson") and r.get("afterJson") for r in update_rows
        ), "UPDATE audit missing beforeJson or afterJson"

    # ------------------------------------------------------------------
    # 6. DELETE — beforeJson present, afterJson is null
    # ------------------------------------------------------------------

    async def test_delete_schedule_has_before_no_after(self, client, dbSession) -> None:
        """DELETE audit record has beforeJson but afterJson is null."""
        await _seedAgent(dbSession, "AUDIT_SCHED_DEL2")
        create_resp = await client.post(
            "/api/v1/agents/AUDIT_SCHED_DEL2/schedules",
            json=_schedulePayload(),
            headers=AUTH_ADMIN,
        )
        schedule_id = create_resp.json()["id"]

        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)

        await client.delete(
            f"/api/v1/agents/AUDIT_SCHED_DEL2/schedules/{schedule_id}",
            headers=AUTH_ADMIN,
        )
        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_schedule&action=DELETE",
            headers=AUTH_ADMIN,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [
            r for r in rows if r["entityId"] == schedule_id and r["action"] == "DELETE"
        ]
        assert len(del_rows) >= 1, "No DELETE audit found"
        assert del_rows[0].get("beforeJson") is not None, "DELETE audit missing beforeJson"
        assert del_rows[0].get("afterJson") is None, "DELETE audit should have null afterJson"
