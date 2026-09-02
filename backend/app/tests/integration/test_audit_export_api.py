"""audit_log export API integration tests."""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


async def drainOutbox(dbSession) -> int:
    """跑一轮 outbox 消费（feat-audit-outbox：审计异步落地）。"""
    from app.workers.audit_worker import AuditWorker

    return await AuditWorker().drainOnce(dbSession)


@pytest.mark.asyncio
class TestAuditExportApi:

    async def test_export_csv_returns_200_with_streaming(self, client, dbSession) -> None:
        """GET /api/v1/audit/export?format=csv returns StreamingResponse with text/csv."""
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "text/csv" in resp.headers["content-type"]
        assert "attachment" in resp.headers.get("content-disposition", "")
        # CSV should have header row
        content = resp.text
        lines = content.strip().split("\n")
        assert len(lines) >= 1
        assert "id" in lines[0].lower()

    async def test_export_jsonl_returns_200(self, client, dbSession) -> None:
        """GET /api/v1/audit/export?format=json returns application/x-ndjson."""
        resp = await client.get(
            "/api/v1/audit/export?format=json",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "application/x-ndjson" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        if lines and lines[0]:
            import json
            obj = json.loads(lines[0])
            assert "id" in obj

    async def test_export_csv_contains_expected_columns(self, client, dbSession) -> None:
        """CSV header: id,created_at,entity_type,entity_id,action,actor,actor_departments,before_json,after_json."""
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        header = resp.text.split("\n")[0]
        for col in ["id", "created_at", "entity_type", "action", "actor"]:
            assert col in header.lower(), f"Missing column: {col}"

    async def test_export_non_admin_returns_403(self, client, dbSession) -> None:
        """Non-admin user gets 403 on export endpoint."""
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_export_csv_properly_escapes_special_chars(self, client, dbSession) -> None:
        """Audit row with comma/quote/newline in kpiName must round-trip via csv.reader.

        Regression test for csv escaping bug: prior implementation used
        ','.join([...]) which corrupted fields containing commas, quotes,
        or newlines. csv.writer must properly RFC 4180-escape such fields.
        """
        import csv
        import io

        # Create KPI with special chars in kpiName (persisted into after_json → audit_log)
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_CSV_{id(self)}",
                "kpiName": "CSV escape test, with \"quote\" and \nnewline",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        await drainOutbox(dbSession)

        # Export CSV and round-trip via csv.reader
        resp = await client.get(
            "/api/v1/audit/export?format=csv",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        reader = csv.reader(io.StringIO(resp.text))
        rows = list(reader)
        assert len(rows) >= 2, f"Expected header + at least one data row, got {len(rows)} rows"
        # Header check
        assert "after_json" in rows[0], f"Missing after_json in header: {rows[0]}"
        # Find a row whose after_json field contains our special-char string.
        # after_json column is index 8 (last column).
        after_json_idx = rows[0].index("after_json")
        special_row = None
        for r in rows[1:]:
            if len(r) > after_json_idx and "CSV escape test" in r[after_json_idx]:
                special_row = r
                break
        assert special_row is not None, (
            f"Expected row with special-char kpiName in after_json column. "
            f"Rows: {rows[1:]}"
        )
        # Confirm the field round-trips intact: commas, quotes, newlines preserved.
        # after_json is JSON-serialized, so inner quotes become \" and \n becomes \\n.
        assert "CSV escape test, with \\\"quote\\\" and \\nnewline" in special_row[after_json_idx]
