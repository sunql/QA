"""data_quality_score_service audit writes — 6 cases (CREATE x 3 + UPDATE x 2 + DELETE x 1)."""
from __future__ import annotations

import pytest
from fastapi import status

from app.tests.integration.test_data_quality_api import _createTestDatasource
from app.tests.integration.test_data_quality_eval_api import (
    _FakeAdapter,
    _installFakeAdapter,
)
from app.workers.audit_worker import AuditWorker

ADMIN_HEADERS = {"X-User-Id": "audit-dq-admin", "X-User-Roles": "admin"}


def _entityToDict(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


async def _createRule(client, ds_id: int, **overrides) -> int:
    """Create a DQ rule and return its id."""
    payload = {
        "ruleName": overrides.get("ruleName", "审计规则"),
        "ruleCode": overrides.get("ruleCode", f"AUD_RULE_{id(overrides)}"),
        "datasourceId": ds_id,
        "targetTable": overrides.get("targetTable", "PORDER"),
        "targetColumn": overrides.get("targetColumn", "ORDER_QTY"),
        "ruleType": overrides.get("ruleType", "COMPLETENESS"),
        "threshold": overrides.get("threshold", "0.00"),
        "severity": overrides.get("severity", "MEDIUM"),
    }
    resp = await client.post(
        "/api/v1/data-quality/rules", json=payload, headers=ADMIN_HEADERS
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
class TestDataQualityScoreAudit:
    """6 cases matching document_audit pattern."""

    async def test_compute_creates_audit_records(
        self, client, dbSession, monkeypatch
    ) -> None:
        """CREATE: each computed score (TABLE + GLOBAL) generates an audit record."""
        ds_id = await _createTestDatasource(client)
        await _createRule(client, ds_id, ruleCode=f"AUD_R1_{id(self)}")

        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        body = resp.json()
        assert body["savedScores"] == 2  # 1 TABLE + 1 GLOBAL

        # Drain outbox / direct audit
        await AuditWorker().drainOnce(dbSession)

        # Verify audit records exist for data_quality_score entity
        audit_resp = await client.get(
            "/api/v1/audit?entity_type=data_quality_score&action=CREATE",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        assert len(rows) >= 2, f"Expected >=2 CREATE records, got {len(rows)}"
        assert all(r["action"] == "CREATE" for r in rows)
        assert all(r["entityType"] == "data_quality_score" for r in rows)

    async def test_compute_actor_departments_injected(
        self, client, dbSession, monkeypatch
    ) -> None:
        """CREATE: actor_departments from X-User-Departments header is recorded."""
        ds_id = await _createTestDatasource(client)
        await _createRule(client, ds_id, ruleCode=f"AUD_R2_{id(self)}")

        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(
            "/api/v1/data-quality/scores/compute",
            headers={**ADMIN_HEADERS, "X-User-Departments": "procurement,quality"},
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        await AuditWorker().drainOnce(dbSession)

        audit_resp = await client.get(
            "/api/v1/audit?entity_type=data_quality_score",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        matching = [
            r
            for r in rows
            if r["action"] == "CREATE"
            and r.get("actorDepartments")
            and "procurement" in r["actorDepartments"]
        ]
        assert len(matching) >= 1, (
            f"Expected >=1 record with actorDepartments containing 'procurement', "
            f"got {matching}"
        )

    async def test_compute_creates_have_after_json(
        self, client, dbSession, monkeypatch
    ) -> None:
        """CREATE: afterJson is populated (beforeJson is None for CREATE)."""
        ds_id = await _createTestDatasource(client)
        await _createRule(client, ds_id, ruleCode=f"AUD_R3_{id(self)}")

        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        resp = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        score_ids = [s["id"] for s in resp.json()["scores"]]
        await AuditWorker().drainOnce(dbSession)

        # Check CREATE records have afterJson, no beforeJson
        for score_id in score_ids:
            audit_resp = await client.get(
                f"/api/v1/audit?entity_type=data_quality_score&action=CREATE&entity_id={score_id}",
                headers=ADMIN_HEADERS,
            )
            rows = audit_resp.json()["rows"]
            create_rows = [r for r in rows if r["action"] == "CREATE"]
            assert len(create_rows) >= 1
            for r in create_rows:
                assert r.get("afterJson") is not None, (
                    f"CREATE record should have afterJson for score {score_id}"
                )
                assert r.get("beforeJson") is None, (
                    f"CREATE record should NOT have beforeJson for score {score_id}"
                )

    async def test_recompute_writes_update_audit_for_prior_rows(
        self, client, dbSession, monkeypatch
    ) -> None:
        """COMPUTE x2: the second compute's new rows get UPDATE audit (prior rows = before state).

        Since each compute creates new rows (append-only history), the new row's
        audit is labeled UPDATE because we know the prior state for the same
        (target_table, score_type) pair. The new row itself carries updated values.
        """
        ds_id = await _createTestDatasource(client)
        await _createRule(client, ds_id, ruleCode=f"AUD_R4_{id(self)}")

        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        # First compute: 2 scores (TABLE + GLOBAL)
        r1 = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert r1.status_code == status.HTTP_200_OK, r1.text
        first_ids = [s["id"] for s in r1.json()["scores"]]
        await AuditWorker().drainOnce(dbSession)

        # Second compute: new rows with different IDs
        r2 = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert r2.status_code == status.HTTP_200_OK, r2.text
        second_ids = [s["id"] for s in r2.json()["scores"]]
        await AuditWorker().drainOnce(dbSession)

        # Verify second IDs are different (new rows created)
        assert all(sid not in first_ids for sid in second_ids)

        # The second compute's new rows should have UPDATE audit
        # (prior state is known, new rows carry updated values)
        for sid in second_ids:
            audit_resp = await client.get(
                f"/api/v1/audit?entity_type=data_quality_score&action=UPDATE&entity_id={sid}",
                headers=ADMIN_HEADERS,
            )
            rows = audit_resp.json()["rows"]
            update_rows = [r for r in rows if r["action"] == "UPDATE"]
            assert len(update_rows) >= 1, (
                f"Second compute new score {sid} should have UPDATE audit"
            )

    async def test_recompute_update_has_before_and_after(
        self, client, dbSession, monkeypatch
    ) -> None:
        """COMPUTE x2: UPDATE audit from second compute has both beforeJson and afterJson."""
        ds_id = await _createTestDatasource(client)
        await _createRule(client, ds_id, ruleCode=f"AUD_R5_{id(self)}")

        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        # First compute
        r1 = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert r1.status_code == status.HTTP_200_OK, r1.text
        await AuditWorker().drainOnce(dbSession)

        # Second compute
        r2 = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert r2.status_code == status.HTTP_200_OK, r2.text
        second_ids = [s["id"] for s in r2.json()["scores"]]
        await AuditWorker().drainOnce(dbSession)

        # Verify UPDATE records have both before and after
        for sid in second_ids:
            audit_resp = await client.get(
                f"/api/v1/audit?entity_type=data_quality_score&action=UPDATE&entity_id={sid}",
                headers=ADMIN_HEADERS,
            )
            rows = audit_resp.json()["rows"]
            update_rows = [r for r in rows if r["action"] == "UPDATE"]
            assert len(update_rows) >= 1
            assert any(
                r.get("beforeJson") and r.get("afterJson")
                for r in update_rows
            ), f"UPDATE for score {sid} should have both beforeJson and afterJson"

    async def test_delete_score_writes_audit(
        self, client, dbSession, monkeypatch
    ) -> None:
        """DELETE: deleteScore() writes DELETE audit with beforeJson and no afterJson."""
        ds_id = await _createTestDatasource(client)
        await _createRule(client, ds_id, ruleCode=f"AUD_R6_{id(self)}")

        adapter = _FakeAdapter(rows_by_marker={"COUNT(": [{"total": 10, "passed": 10}]})
        _installFakeAdapter(monkeypatch, adapter)

        # Create score
        r1 = await client.post(
            "/api/v1/data-quality/scores/compute", headers=ADMIN_HEADERS
        )
        assert r1.status_code == status.HTTP_200_OK, r1.text
        score_id = r1.json()["scores"][0]["id"]
        await AuditWorker().drainOnce(dbSession)

        # Delete via service method (deleteScore writes DELETE audit)
        from app.services.data_quality_score_service import DataQualityScoreService
        svc = DataQualityScoreService()
        await svc.deleteScore(dbSession, score_id, actor="audit-dq-admin")
        await AuditWorker().drainOnce(dbSession)

        # Verify DELETE audit record
        audit_resp = await client.get(
            f"/api/v1/audit?entity_type=data_quality_score&action=DELETE&entity_id={score_id}",
            headers=ADMIN_HEADERS,
        )
        rows = audit_resp.json()["rows"]
        del_rows = [r for r in rows if r["entityId"] == score_id and r["action"] == "DELETE"]
        assert len(del_rows) >= 1
        assert del_rows[0].get("beforeJson") is not None
        assert del_rows[0].get("afterJson") is None
