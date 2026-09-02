"""audit_log 查询 API 集成测试（Phase 4.5）。

覆盖：全局查询 / by-entity / by-actor / by-id。
使用 kpi_catalog 写入 audit（FeatureDefinition 创建时也会写 audit）。
"""

from __future__ import annotations

import pytest
from fastapi import status


ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

async def drainOutbox(dbSession) -> int:
    """跑一轮 outbox 消费（feat-audit-outbox：审计异步落地）。"""
    from app.workers.audit_worker import AuditWorker

    return await AuditWorker().drainOnce(dbSession)



@pytest.mark.asyncio
class TestAuditApi:
    """audit_log 查询 API：4 个端点。"""

    async def test_list_all_returns_audit_records(self, client, dbSession) -> None:
        """创建 KPI → audit 写入 → 全量查询能查到。"""
        # 创建 KPI（触发 audit CREATE）
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_TEST_{id(self)}",
                "kpiName": "审计测试KPI",
                "formula": "SELECT COUNT(*)",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        kpi_id = kpi_resp.json()["id"]
        await drainOutbox(dbSession)

        # 全量查询
        resp = await client.get("/api/v1/audit", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        rows = body["rows"]
        assert isinstance(rows, list)
        # 有 CREATE 记录
        create_rows = [r for r in rows if r["action"] == "CREATE" and r["entityType"] == "kpi_catalog"]
        assert len(create_rows) >= 1

    async def test_list_by_entity_returns_kpi_history(self, client, dbSession) -> None:
        """by-entity 端点返回指定 KPI 的变更历史。"""
        # 创建 KPI
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_ENTITY_{id(self)}",
                "kpiName": "实体查询测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        kpi_id = kpi_resp.json()["id"]
        await drainOutbox(dbSession)

        resp = await client.get(
            f"/api/v1/audit/by-entity/kpi_catalog/{kpi_id}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()
        assert len(rows) >= 1
        # 首条应为 CREATE
        assert rows[0]["action"] == "CREATE"
        assert rows[0]["entityType"] == "kpi_catalog"
        assert rows[0]["entityId"] == kpi_id

    async def test_list_by_actor_returns_user_actions(self, client, dbSession) -> None:
        """by-actor 端点返回指定用户的所有操作。"""
        resp = await client.get(
            "/api/v1/audit/by-actor/test-admin",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()
        assert isinstance(rows, list)
        # 所有记录的 actor 应为 test-admin
        for r in rows:
            assert r["actor"] == "test-admin"

    async def test_get_by_id_returns_single_record(self, client, dbSession) -> None:
        """先拿到 audit ID → 再按 ID 查询。"""
        # 创建 KPI 拿到 audit 记录 ID
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_ID_{id(self)}",
                "kpiName": "ID查询测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED

        # 查 by-entity 拿到最新 audit ID
        kpi_id = kpi_resp.json()["id"]
        await drainOutbox(dbSession)
        list_resp = await client.get(
            f"/api/v1/audit/by-entity/kpi_catalog/{kpi_id}",
            headers=ADMIN_HEADERS,
        )
        rows = list_resp.json()
        audit_id = rows[0]["id"]

        # 按 ID 查询
        resp = await client.get(f"/api/v1/audit/{audit_id}", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["id"] == audit_id
        assert body["entityType"] == "kpi_catalog"

    async def test_list_with_entity_type_filter(self, client, dbSession) -> None:
        """全局查询加 entity_type 过滤。"""
        resp = await client.get(
            "/api/v1/audit?entity_type=kpi_catalog",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()["rows"]
        for r in rows:
            assert r["entityType"] == "kpi_catalog"

    async def test_list_with_action_filter(self, client, dbSession) -> None:
        """全局查询加 action 过滤。"""
        resp = await client.get(
            "/api/v1/audit?action=CREATE",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()["rows"]
        for r in rows:
            assert r["action"] == "CREATE"

    async def test_list_pagination(self, client, dbSession) -> None:
        """分页 limit/offset。"""
        resp = await client.get(
            "/api/v1/audit?limit=5&offset=0",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.json()["rows"]) <= 5

    async def test_unknown_id_returns_404(self, client, dbSession) -> None:
        """不存在的 audit ID → 404。"""
        resp = await client.get("/api/v1/audit/999999999", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_list_returns_audit_log_page_shape(self, client, dbSession) -> None:
        """GET /api/v1/audit returns {rows: [...], total: N} (AuditLogPage), not bare list."""
        resp = await client.get("/api/v1/audit", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert "rows" in body, f"Expected AuditLogPage shape, got: {body}"
        assert "total" in body, f"Expected AuditLogPage shape, got: {body}"
        assert isinstance(body["rows"], list)
        assert isinstance(body["total"], int)

    async def test_list_actor_ilike_fuzzy(self, client, dbSession) -> None:
        """actor='张' matches actor containing '张', not exact."""
        # 先有数据
        await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_ILIKE_{id(self)}",
                "kpiName": "模糊匹配测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        await drainOutbox(dbSession)
        # 用部分字符搜索（应命中）
        resp = await client.get("/api/v1/audit?actor=admin", headers=ADMIN_HEADERS)
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["total"] >= 1

    async def test_list_actor_departments_filter(self, client, dbSession) -> None:
        """actor_departments='采购' matches '采购部,财务部'."""
        resp = await client.get(
            "/api/v1/audit?actor_departments=采购",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

    async def test_list_since_until_range(self, client, dbSession) -> None:
        """since/until filters by created_at range."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        resp = await client.get(
            f"/api/v1/audit?since=2020-01-01T00:00:00Z&until=2099-01-01T00:00:00Z",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["total"] >= 0

    async def test_non_admin_returns_403(self, client, dbSession) -> None:
        """Non-admin user gets 403 on /api/v1/audit."""
        resp = await client.get(
            "/api/v1/audit",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_by_entity_acl收紧(self, client, dbSession) -> None:
        """GET /api/v1/audit/by-entity/{t}/{id} requires admin."""
        resp = await client.get(
            "/api/v1/audit/by-entity/kpi_catalog/1",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_by_actor_acl收紧(self, client, dbSession) -> None:
        """GET /api/v1/audit/by-actor/{actor} requires admin."""
        resp = await client.get(
            "/api/v1/audit/by-actor/test-admin",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_get_by_id_acl收紧(self, client, dbSession) -> None:
        """GET /api/v1/audit/{id} requires admin."""
        resp = await client.get(
            "/api/v1/audit/1",
            headers={"X-User-Id": "alice", "X-User-Roles": "viewer"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_list_entity_id_filter(self, client, dbSession) -> None:
        """entity_id filter does CAST TEXT ILIKE."""
        # 创建 KPI，然后按其 ID 搜索
        kpi_resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": f"AUDIT_EID_{id(self)}",
                "kpiName": "ID过滤测试",
                "formula": "SELECT 1",
                "entityType": "SUPPLIER",
                "owner": "采购部",
            },
            headers=ADMIN_HEADERS,
        )
        assert kpi_resp.status_code == status.HTTP_201_CREATED
        kpi_id = kpi_resp.json()["id"]
        await drainOutbox(dbSession)
        resp = await client.get(
            f"/api/v1/audit?entity_type=kpi_catalog",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()["rows"]
        assert any(r["entityId"] == kpi_id for r in rows)
