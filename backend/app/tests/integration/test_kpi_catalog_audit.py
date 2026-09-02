"""kpi_catalog_service audit writes — 6 cases (CREATE/UPDATE/DELETE x success/actor_departments)."""
from __future__ import annotations
import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "audit-kpi-admin", "X-User-Roles": "admin"}


@pytest.mark.asyncio
class TestKpiCatalogAudit:
    """6 cases: CREATE success / UPDATE success / DELETE success / actor_departments injected."""

    async def test_create_kpi_writes_audit(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_CREATE_{id(self)}",
                "kpiName": "审计测试KPI",
                "formula": "SELECT COUNT(*)",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        kpi_id = resp.json()["id"]
        # Check audit — drain outbox first
        from app.workers.audit_worker import AuditWorker

        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == kpi_id and r["action"] == "CREATE" for r in rows)

    async def test_update_kpi_writes_audit(self, client, dbSession) -> None:
        # Create then update
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_UPDATE_{id(self)}",
                "kpiName": "原始名称",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker

        await AuditWorker().drainOnce(dbSession)
        update_resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi_id}",
            json={"kpiName": "新名称"},
            headers=ADMIN_HEADERS,
        )
        assert update_resp.status_code == status.HTTP_200_OK
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=UPDATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == kpi_id and r["action"] == "UPDATE" for r in rows)

    async def test_delete_kpi_writes_audit(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_DEL_{id(self)}",
                "kpiName": "待删除",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker

        await AuditWorker().drainOnce(dbSession)
        del_resp = await client.delete(
            f"/api/v1/kpi-catalog/{kpi_id}", headers=ADMIN_HEADERS
        )
        assert del_resp.status_code == status.HTTP_204_NO_CONTENT
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert any(r["entityId"] == kpi_id and r["action"] == "DELETE" for r in rows)

    async def test_create_kpi_actor_departments_injected(self, client, dbSession) -> None:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_DEPT_{id(self)}",
                "kpiName": "部门审计",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "procurement",
            },
            headers={**ADMIN_HEADERS, "X-User-Departments": "procurement,finance"},
        )
        assert resp.status_code == status.HTTP_201_CREATED
        from app.workers.audit_worker import AuditWorker

        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r
            for r in rows
            if r["action"] == "CREATE" and "procurement" in (r.get("actorDepartments") or "")
        ]
        assert len(matching) >= 1

    async def test_update_kpi_has_before_and_after(self, client, dbSession) -> None:
        # Create KPI
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_BEFORE_{id(self)}",
                "kpiName": "原始",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker

        await AuditWorker().drainOnce(dbSession)
        # Update
        await client.put(
            f"/api/v1/kpi-catalog/{kpi_id}",
            json={"kpiName": "已更新"},
            headers=ADMIN_HEADERS,
        )
        await AuditWorker().drainOnce(dbSession)
        # Check UPDATE record has both before and after
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=UPDATE&entity_id={kpi_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        update_rows = [r for r in rows if r["action"] == "UPDATE"]
        assert len(update_rows) >= 1
        # At least one UPDATE should have both beforeJson and afterJson
        assert any(r.get("beforeJson") and r.get("afterJson") for r in update_rows)

    async def test_delete_kpi_has_before_no_after(self, client, dbSession) -> None:
        # Create then delete
        create_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUD_KPI_DEL2_{id(self)}",
                "kpiName": "删除测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        kpi_id = create_resp.json()["id"]
        from app.workers.audit_worker import AuditWorker

        await AuditWorker().drainOnce(dbSession)
        await client.delete(f"/api/v1/kpi-catalog/{kpi_id}", headers=ADMIN_HEADERS)
        await AuditWorker().drainOnce(dbSession)
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog&action=DELETE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [
            r for r in rows if r["entityId"] == kpi_id and r["action"] == "DELETE"
        ]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
