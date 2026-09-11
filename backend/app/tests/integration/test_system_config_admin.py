"""system_config admin API integration tests.

覆盖：
- GET 全量列表（任何登录用户）
- GET 按 key（admin-only，404 if not found）
- PUT 更新 value（admin-only，同事务 audit_log 写入）
- 403（非 admin）/ 404（未知 key）
- audit_log 断言

每个测试 arrange 通过直接 SQL INSERT 种子（_pg_support TRUNCATE 清库；conftest
自动 warmUp 业务缓存但不动 system_config，本测试自行负责该表数据）。
"""

from __future__ import annotations

import pytest
from fastapi import status
from sqlalchemy import select, text

ADMIN_HEADERS = {"X-User-Id": "sysconfig-admin", "X-User-Roles": "admin"}
NON_ADMIN_HEADERS = {"X-User-Id": "alice", "X-User-Roles": "viewer"}
SEED_KEY = "ENABLE_L4_AGENT_LOOP"
SEED_DESCRIPTION = "L4 LangGraph agent loop switch"


async def _seedSystemConfig(dbSession, key: str, value: str | None = "false") -> None:
    """每个测试 arrange 阶段向 system_config 表 INSERT 一行 seed。

    conftest TRUNCATE 后表为空；本测试需自行恢复 ENABLE_L4_AGENT_LOOP 行。
    """
    await dbSession.execute(
        text(
            "INSERT INTO system_config (key, value, description) "
            "VALUES (:k, :v, :d) ON CONFLICT (key) DO UPDATE "
            "SET value = EXCLUDED.value, description = EXCLUDED.description, "
            "updated_time = NOW()"
        ),
        {"k": key, "v": value, "d": SEED_DESCRIPTION},
    )
    await dbSession.commit()


@pytest.mark.asyncio
class TestSystemConfigAdmin:
    """system_config admin API — 8 cases (RBAC + CRUD + audit)。"""

    async def test_list_returns_at_least_one_row(self, client, dbSession) -> None:
        """GET /admin/system-config 至少含 ENABLE_L4_AGENT_LOOP。"""
        await _seedSystemConfig(dbSession, SEED_KEY)
        resp = await client.get(
            "/api/v1/admin/system-config",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        rows = resp.json()
        assert isinstance(rows, list)
        keys = [r["key"] for r in rows]
        assert SEED_KEY in keys

    async def test_list_non_admin_allowed(self, client, dbSession) -> None:
        """GET 列表对非 admin 也开放（admin-only 限制走 menu grant 隐式约束）。"""
        await _seedSystemConfig(dbSession, SEED_KEY)
        resp = await client.get(
            "/api/v1/admin/system-config",
            headers=NON_ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK

    async def test_get_by_key_admin(self, client, dbSession) -> None:
        """GET /admin/system-config/{key} admin 返回 200。"""
        await _seedSystemConfig(dbSession, SEED_KEY)
        resp = await client.get(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()
        assert body["key"] == SEED_KEY
        assert "value" in body
        assert "description" in body
        assert "updatedTime" in body

    async def test_get_by_key_non_admin_forbidden(self, client, dbSession) -> None:
        """GET /admin/system-config/{key} 非 admin 返回 403。"""
        await _seedSystemConfig(dbSession, SEED_KEY)
        resp = await client.get(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            headers=NON_ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_get_unknown_key_returns_404(self, client, dbSession) -> None:
        """GET /admin/system-config/{unknown} → 404。"""
        resp = await client.get(
            "/api/v1/admin/system-config/NONEXISTENT_KEY_XYZ",
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_put_update_value_writes_audit(self, client, dbSession) -> None:
        """PUT 更新 value：admin 成功 + 同事务 audit_log 写入 + 二次 GET 确认新值。"""
        await _seedSystemConfig(dbSession, SEED_KEY, value="false")

        # 1) 读初始值
        before_resp = await client.get(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            headers=ADMIN_HEADERS,
        )
        original_value = before_resp.json()["value"]

        # 2) PUT 改 value（确保新值 ≠ 原值）
        new_value = "true" if original_value != "true" else "false"
        put_resp = await client.put(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            json={"value": new_value},
            headers=ADMIN_HEADERS,
        )
        assert put_resp.status_code == status.HTTP_200_OK
        updated = put_resp.json()
        assert updated["key"] == SEED_KEY
        assert updated["value"] == new_value

        # 3) 二次 GET 确认持久化
        after_resp = await client.get(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            headers=ADMIN_HEADERS,
        )
        assert after_resp.json()["value"] == new_value

        # 4) audit_log 断言：entity_type='system_config' / action='UPDATE'
        # 直接 SQL 查（绕开 outbox 异步；本测试用 record() 同事务直写路径）
        from app.domain.models import AuditLog

        stmt = (
            select(AuditLog)
            .where(
                AuditLog.entity_type == "system_config",
                AuditLog.action == "UPDATE",
            )
            .order_by(AuditLog.id.desc())
            .limit(5)
        )
        audits = (await dbSession.execute(stmt)).scalars().all()
        latest = audits[0]
        assert latest.actor == "sysconfig-admin"
        assert latest.before_json == {"value": original_value}
        assert latest.after_json == {"value": new_value}

    async def test_put_non_admin_forbidden(self, client, dbSession) -> None:
        """PUT 非 admin → 403。"""
        await _seedSystemConfig(dbSession, SEED_KEY)
        resp = await client.put(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            json={"value": "true"},
            headers=NON_ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    async def test_put_unknown_key_returns_404(self, client, dbSession) -> None:
        """PUT /admin/system-config/{unknown} → 404。"""
        resp = await client.put(
            "/api/v1/admin/system-config/NONEXISTENT_KEY_XYZ",
            json={"value": "true"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    async def test_put_null_value_clears(self, client, dbSession) -> None:
        """PUT value=null 允许（业务表达"清空/未配置"）。"""
        await _seedSystemConfig(dbSession, SEED_KEY, value="false")
        put_resp = await client.put(
            f"/api/v1/admin/system-config/{SEED_KEY}",
            json={"value": None},
            headers=ADMIN_HEADERS,
        )
        assert put_resp.status_code == status.HTTP_200_OK
        assert put_resp.json()["value"] is None