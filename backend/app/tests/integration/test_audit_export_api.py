"""audit_log export API integration tests."""
from __future__ import annotations

import pytest
from fastapi import status

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


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
