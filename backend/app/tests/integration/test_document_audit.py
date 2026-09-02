"""document_service audit writes — 6 cases (CREATE/UPDATE/DELETE x success/actor_departments)."""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-doc-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
class TestDocumentAudit:
    """6 cases: CREATE success / UPDATE success / DELETE success / actor_departments injected."""

    async def test_create_document_writes_audit(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/documents",
            json={
                "documentId": f"AUD_DOC_CREATE_{id(self)}",
                "documentName": "审计测试文档",
                "documentType": "CONTRACT",
                "version": "v1.0",
                "owner": "采购部",
                "securityLevel": "L2",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        doc_id = resp.json()["id"]
        # Drain outbox
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=document_catalog&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == doc_id and r["action"] == "CREATE" for r in rows)

    async def test_update_document_writes_audit(self, client, dbSession) -> None:
        # Create then update
        create_resp = await client.post(
            "/api/v1/documents",
            json={
                "documentId": f"AUD_DOC_UPDATE_{id(self)}",
                "documentName": "原始名称",
                "documentType": "CONTRACT",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        doc_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        update_resp = await client.put(
            f"/api/v1/documents/{doc_id}",
            json={"documentName": "新名称", "version": "v2.0"},
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=document_catalog&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == doc_id and r["action"] == "UPDATE" for r in rows)

    async def test_delete_document_writes_audit(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/documents",
            json={
                "documentId": f"AUD_DOC_DEL_{id(self)}",
                "documentName": "待删除",
                "documentType": "CONTRACT",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        doc_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        del_resp = await client.delete(
            f"/api/v1/documents/{doc_id}", headers=ADMIN_HEADERS
        )
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=document_catalog&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == doc_id and r["action"] == "DELETE" for r in rows)

    async def test_create_document_actor_departments_injected(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/documents",
            json={
                "documentId": f"AUD_DOC_DEPT_{id(self)}",
                "documentName": "部门审计",
                "documentType": "CONTRACT",
                "owner": "采购部",
            },
            headers={**ADMIN_HEADERS, "X-User-Departments": "procurement,finance"},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=document_catalog",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r
            for r in rows
            if r["action"] == "CREATE" and "procurement" in (r.get("actorDepartments") or "")
        ]
        assert len(matching) >= 1

    async def test_update_document_has_before_and_after(self, client, dbSession) -> None:
        # Create document
        create_resp = await client.post(
            "/api/v1/documents",
            json={
                "documentId": f"AUD_DOC_BEFORE_{id(self)}",
                "documentName": "原始",
                "documentType": "CONTRACT",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        doc_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        # Update
        await client.put(
            f"/api/v1/documents/{doc_id}",
            json={"documentName": "已更新"},
            headers=ADMIN_HEADERS,
        )
        await AuditWorker().drainOnce(dbSession)
        # Check UPDATE record has both beforeJson and afterJson
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=document_catalog&action=UPDATE&entity_id={doc_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_delete_document_has_before_no_after(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/documents",
            json={
                "documentId": f"AUD_DOC_DEL2_{id(self)}",
                "documentName": "删除测试",
                "documentType": "CONTRACT",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        doc_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker
        await AuditWorker().drainOnce(dbSession)
        await client.delete(f"/api/v1/documents/{doc_id}", headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=document_catalog&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [
            r for r in rows if r["entityId"] == doc_id and r["action"] == "DELETE"
        ]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
