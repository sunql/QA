"""agent_registry_service audit writes — 8 cases (CREATE/UPDATE/DEPRECATE/DELETE x success/actor_departments)."""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-agent-admin", "X-User-Roles": "admin"}


def _make_payload(code: str = "AUD_AGENT_001", **overrides) -> dict:
    payload = {
        "agentCode": code,
        "agentName": "审计测试Agent",
        "description": "audit test",
        "triggerType": "user_question",
        "responseLatency": "realtime",
        "dataDomains": ["PROCUREMENT"],
        "dataLayers": ["FEATURE"],
        "status": "draft",
        "version": "v1.0",
        "policies": [],
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
class TestAgentRegistryAudit:
    """8 cases: CREATE/UPDATE/DEPRECATE/DELETE success + actor_departments injected."""

    async def test_create_agent_writes_audit(self, client, dbSession) -> None:
        code = f"AUD_CR_{id(self)}"
        resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        agent_id = resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_definition&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == agent_id and r["action"] == "CREATE" for r in rows)

    async def test_update_agent_writes_audit(self, client, dbSession) -> None:
        code = f"AUD_UP_{id(self)}"
        create_resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers=ADMIN_HEADERS,
        )
        agent_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        update_resp = await client.put(
            f"/api/v1/agents/{code}",
            json={"description": "updated desc"},
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_definition&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == agent_id and r["action"] == "UPDATE" for r in rows)

    async def test_deprecate_agent_writes_update_audit(self, client, dbSession) -> None:
        code = f"AUD_DEP_{id(self)}"
        create_resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers=ADMIN_HEADERS,
        )
        agent_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        del_resp = await client.delete(
            f"/api/v1/agents/{code}",
            headers=ADMIN_HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        # deprecateAgent uses action=UPDATE (not separate DEPRECATE action)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_definition&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == agent_id and r["action"] == "UPDATE" for r in rows)

    async def test_delete_agent_not_exposed_in_api(self, client, dbSession) -> None:
        """不带 hard_delete 的 DELETE = 软删除（status → deprecated），非硬删除。

        契约演进：本用例曾断言 DELETE 端点 405/404 不暴露；后来 DELETE 被实现为
        软删除 API 面（test_agent_registry_api.py 文件头第 8 行 + deprecate 用例），
        硬删除改为 `?hard_delete=true` 显式参数。这里钉住：默认路径绝不能删行。
        """
        create_resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(f"AUD_AGENT_NODEL_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        assert create_resp.status_code in (200, 201), f"setup create failed: {create_resp.text}"
        agent_code = create_resp.json()["agentCode"]
        del_resp = await client.delete(f"/api/v1/agents/{agent_code}", headers=ADMIN_HEADERS)
        assert del_resp.status_code == 200, (
            f"soft delete should succeed; got {del_resp.status_code}: {del_resp.text}"
        )
        assert del_resp.json()["status"] == "deprecated"

    async def test_create_agent_actor_departments_injected(self, client, dbSession) -> None:
        code = f"AUD_DEPT_{id(self)}"
        resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers={**ADMIN_HEADERS, "X-User-Departments": "CaiGou,Finance"},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_definition",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r for r in rows
            if r["action"] == "CREATE" and "CaiGou" in (r.get("actorDepartments") or "")
        ]
        assert len(matching) >= 1

    async def test_update_agent_has_before_and_after(self, client, dbSession) -> None:
        code = f"AUD_BEFORE_{id(self)}"
        create_resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers=ADMIN_HEADERS,
        )
        agent_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        await client.put(
            f"/api/v1/agents/{code}",
            json={"description": "changed"},
            headers=ADMIN_HEADERS,
        )
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=agent_definition&action=UPDATE&entity_id={agent_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_deprecate_agent_has_before_and_after(self, client, dbSession) -> None:
        code = f"AUD_DEP2_{id(self)}"
        create_resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers=ADMIN_HEADERS,
        )
        agent_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        await client.delete(f"/api/v1/agents/{code}", headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=agent_definition&action=UPDATE&entity_id={agent_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        # deprecate updates status field
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_delete_agent_has_before_no_after(self, client, dbSession) -> None:
        code = f"AUD_DEL2_{id(self)}"
        create_resp = await client.post(
            "/api/v1/agents",
            json=_make_payload(code),
            headers=ADMIN_HEADERS,
        )
        agent_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        del_resp = await client.delete(
            f"/api/v1/agents/{code}?hard_delete=true",
            headers=ADMIN_HEADERS,
        )
        assert del_resp.status_code == status.HTTP_200_OK, f"hard delete failed: {del_resp.text}"
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=agent_definition&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [
            r for r in rows if r["entityId"] == agent_id and r["action"] == "DELETE"
        ]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
