"""datasource_service audit writes — 6 cases (CREATE/UPDATE/DELETE x success/actor_departments)."""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-ds-admin", "X-User-Roles": "admin"}


def _make_payload(name: str = "ds-audit", **overrides) -> dict:
    payload = {
        "name": name,
        "type": "postgresql",
        "host": "db.example.com",
        "port": 5432,
        "databaseName": "appdb",
        "username": "u",
        "password": "p",
        "description": None,
        "isActive": True,
        "isDefault": False,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
class TestDataSourceAudit:
    """6 cases: CREATE success / UPDATE success / DELETE success / actor_departments injected."""

    async def test_create_datasource_writes_audit(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/datasources",
            json=_make_payload(f"AUD_DS_CREATE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        ds_id = resp.json()["id"]
        # Drain outbox first
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_source&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == ds_id and r["action"] == "CREATE" for r in rows)

    async def test_update_datasource_writes_audit(self, client, dbSession) -> None:
        # Create then update
        create_resp = await client.post(
            "/api/v1/datasources",
            json=_make_payload(f"AUD_DS_UPDATE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        ds_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        update_resp = await client.put(
            f"/api/v1/datasources/{ds_id}",
            json={"description": "updated desc"},
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_source&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == ds_id and r["action"] == "UPDATE" for r in rows)

    async def test_delete_datasource_writes_audit(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/datasources",
            json=_make_payload(f"AUD_DS_DEL_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        ds_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        del_resp = await client.delete(f"/api/v1/datasources/{ds_id}", headers=ADMIN_HEADERS)
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_source&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == ds_id and r["action"] == "DELETE" for r in rows)

    async def test_create_datasource_actor_departments_injected(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/datasources",
            json=_make_payload(f"AUD_DS_DEPT_{id(self)}"),
            headers={**ADMIN_HEADERS, "X-User-Departments": "CaiGou,Finance"},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_source",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [r for r in rows if r["action"] == "CREATE" and "CaiGou" in (r.get("actorDepartments") or "")]
        assert len(matching) >= 1

    async def test_update_datasource_has_before_and_after(self, client, dbSession) -> None:
        # Create datasource
        create_resp = await client.post(
            "/api/v1/datasources",
            json=_make_payload(f"AUD_DS_BEFORE_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        ds_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Update
        await client.put(
            f"/api/v1/datasources/{ds_id}",
            json={"description": "changed"},
            headers=ADMIN_HEADERS,
        )
        await AuditWorker().drainOnce(dbSession)
        # Check UPDATE record has both before and after
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_source&action=UPDATE&entity_id={ds_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        # At least one UPDATE should have both beforeJson and afterJson
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_delete_datasource_has_before_no_after(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/datasources",
            json=_make_payload(f"AUD_DS_DEL2_{id(self)}"),
            headers=ADMIN_HEADERS,
        )
        ds_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        await client.delete(f"/api/v1/datasources/{ds_id}", headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_source&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [r for r in rows if r["entityId"] == ds_id and r["action"] == "DELETE"]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
