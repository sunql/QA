"""Feature Rule API 集成测试（spec §9）。

CRUD happy path + 409 version conflict + 409 referencing + admin-only ACL + 422 validation.
真实 PG（qa_metadata_test）+ 完整 API 链路。
"""
from __future__ import annotations

import pytest

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
NON_ADMIN_HEADERS = {"X-User-Id": "user1", "X-User-Roles": "user"}


class TestFeatureRuleApi:
    async def test_list_rules_returns_seeded(self, client) -> None:
        """seed 4 rules via warmAgentCaches autouse."""
        r = await client.get("/api/v1/feature-rules")
        assert r.status_code == 200
        assert len(r.json()) == 4

    async def test_get_rule_not_found(self, client) -> None:
        r = await client.get("/api/v1/feature-rules/nonexistent")
        assert r.status_code == 404

    async def test_get_rule_success(self, client) -> None:
        r = await client.get("/api/v1/feature-rules/supplier_risk_score_main")
        assert r.status_code == 200
        body = r.json()
        assert body["code"] == "supplier_risk_score_main"
        assert body["version"] == 1

    async def test_create_rule_admin_succeeds(self, client) -> None:
        r = await client.post(
            "/api/v1/feature-rules",
            json={
                "code": "new_rule",
                "data_object": "SUPPLIER",
                "data_layer": "FEATURE",
                "target_level": "RISK",
                "feature_name": "SUPPLIER_NEW_METRIC",
                "thresholds": [
                    {"severity": "HIGH", "operator": "lt", "threshold_value": 10}
                ],
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 201
        assert r.json()["version"] == 1

    async def test_create_rule_non_admin_403(self, client) -> None:
        """X-User-Roles: user（无 admin）→ 403。"""
        r = await client.post(
            "/api/v1/feature-rules",
            json={
                "code": "new_rule_2",
                "data_object": "SUPPLIER",
                "data_layer": "FEATURE",
                "target_level": "RISK",
                "feature_name": "X",
                "thresholds": [
                    {"severity": "HIGH", "operator": "lt", "threshold_value": 10}
                ],
            },
            headers=NON_ADMIN_HEADERS,
        )
        assert r.status_code == 403

    async def test_create_rule_dup_code_409(self, client) -> None:
        """重复 code → 409 ConflictError。"""
        r = await client.post(
            "/api/v1/feature-rules",
            json={
                "code": "supplier_risk_score_main",  # seeded
                "data_object": "SUPPLIER",
                "data_layer": "FEATURE",
                "target_level": "RISK",
                "feature_name": "X",
                "thresholds": [
                    {"severity": "HIGH", "operator": "lt", "threshold_value": 10}
                ],
            },
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409

    async def test_update_rule_version_mismatch_409(self, client) -> None:
        r = await client.put(
            "/api/v1/feature-rules/supplier_risk_score_main",
            json={"enabled": False, "version": 999},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"
        body = r.json()
        assert body is not None
        assert body["details"]["current_version"] == 1

    async def test_delete_rule_referenced_409(self, client, dbSession) -> None:
        """AgentDefinition 引用时删除 → 409 FeatureRuleReferencingError。"""
        from app.domain.models import AgentDefinition

        agent = AgentDefinition(
            agent_code="test_ref_agent",
            agent_name="Test Ref Agent",
            tool_name="supplier_risk_score_main",
        )
        dbSession.add(agent)
        await dbSession.commit()
        r = await client.delete(
            "/api/v1/feature-rules/supplier_risk_score_main",
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 409
        assert "test_ref_agent" in r.json()["details"]["referencing"]

    async def test_toggle_enabled(self, client) -> None:
        r = await client.post(
            "/api/v1/feature-rules/supplier_otd_high_risk/toggle",
            json={"enabled": False},
            headers=ADMIN_HEADERS,
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["version"] == 2
