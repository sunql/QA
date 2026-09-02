"""entity_mapping_service audit writes — 6 cases (CREATE/UPDATE/DELETE x success/actor_departments)."""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-em-admin", "X-User-Roles": "admin"}

_BASE_PAYLOAD = {
    "entityType": "SUPPLIER",
    "enterpriseKey": 100001,
    "enterpriseCode": "SUP000001",
    "sourceSystem": "ERP",
    "sourceKey": "V000001",
    "sourceCode": "V000001",
    "matchRule": "MDM_MASTER",
    "effectiveDate": "2026-01-01",
    "expiryDate": "2099-12-31",
}


def _payload(**overrides):
    p = dict(_BASE_PAYLOAD)
    p.update(overrides)
    return p


@pytest.mark.asyncio
class TestEntityMappingAudit:
    """6 cases: CREATE success / UPDATE success / DELETE success / actor_departments injected / before+after / before-only."""

    async def test_create_entity_mapping_writes_audit(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001 + id(self), enterpriseCode=f"SUP_AUD_CREATE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        mapping_id = resp.json()["id"]
        # Drain outbox
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=entity_mapping&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == mapping_id and r["action"] == "CREATE" for r in rows)

    async def test_update_entity_mapping_writes_audit(self, client, dbSession) -> None:
        # Create
        create_resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001 + id(self), enterpriseCode=f"SUP_AUD_UPDATE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        mapping_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Update
        update_resp = await client.put(
            f"/api/v1/entity-mappings/{mapping_id}",
            json={"sourceCode": "V000001-UPDATED"},
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=entity_mapping&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == mapping_id and r["action"] == "UPDATE" for r in rows)

    async def test_delete_entity_mapping_writes_audit(self, client, dbSession) -> None:
        # Create
        create_resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001 + id(self), enterpriseCode=f"SUP_AUD_DELETE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        mapping_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Delete
        del_resp = await client.delete(f"/api/v1/entity-mappings/{mapping_id}", headers=ADMIN_HEADERS)
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=entity_mapping&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == mapping_id and r["action"] == "DELETE" for r in rows)

    async def test_create_entity_mapping_actor_departments_injected(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001 + id(self), enterpriseCode=f"SUP_AUD_DEPT_{id(self)}"),
            headers={**ADMIN_HEADERS, "X-User-Departments": "procurement,finance"},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=entity_mapping",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [r for r in rows if r["action"] == "CREATE" and r.get("actorDepartments") and "procurement" in r["actorDepartments"]]
        assert len(matching) >= 1

    async def test_update_entity_mapping_has_before_and_after(self, client, dbSession) -> None:
        # Create
        create_resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001 + id(self), enterpriseCode=f"SUP_AUD_BEFORE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        mapping_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Update
        await client.put(
            f"/api/v1/entity-mappings/{mapping_id}",
            json={"sourceCode": "V000001-CHANGED"},
            headers=ADMIN_HEADERS,
        )
        await AuditWorker().drainOnce(dbSession)
        # Check UPDATE record has both before and after
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=entity_mapping&action=UPDATE&entity_id={mapping_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_delete_entity_mapping_has_before_no_after(self, client, dbSession) -> None:
        # Create
        create_resp = await client.post(
            "/api/v1/entity-mappings",
            json=_payload(enterpriseKey=100001 + id(self), enterpriseCode=f"SUP_AUD_DEL2_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        mapping_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Delete
        await client.delete(f"/api/v1/entity-mappings/{mapping_id}", headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=entity_mapping&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [r for r in rows if r["entityId"] == mapping_id and r["action"] == "DELETE"]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
