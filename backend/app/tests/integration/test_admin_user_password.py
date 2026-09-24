"""feat-admin-user-password：admin创建用户密码+重置密码端到端集成测试。

全链路真实 PG (TEST_DATABASE_URL=...5433/qa_metadata_test)；通过 X-User-* stub 头走 admin。
"""
from __future__ import annotations

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import User
from scripts.seed_rbac import seedRbacBaseline

AUTH_ADMIN = {"X-User-Id": "admin", "X-User-Roles": "admin"}


@pytest.fixture(autouse=True)
async def _rbacSeeds(dbSession: AsyncSession) -> None:
    """幂等 seed RBAC 基线：admin 用户/角色 → getAdminOnlyActor 能识别 admin actor。"""
    await seedRbacBaseline(dbSession)


@pytest.mark.asyncio
async def test_admin_create_user_with_password_writes_hash(
    client: AsyncClient, dbSession: AsyncSession
):
    """POST /users 带 password → 201 → password_hash 写入 + must_change_password=True。"""
    resp = await client.post(
        "/api/v1/users",
        json={"username": "alice", "displayName": "Alice", "password": "ValidPass1"},
        headers=AUTH_ADMIN,
    )
    assert resp.status_code == 201, resp.text

    row = (
        await dbSession.execute(select(User).where(User.username == "alice"))
    ).scalar_one()
    assert row.password_hash is not None
    assert bcrypt.checkpw(b"ValidPass1", row.password_hash.encode())
    assert row.must_change_password is True


@pytest.mark.asyncio
async def test_admin_create_user_missing_password_returns_422(client: AsyncClient):
    resp = await client.post(
        "/api/v1/users",
        json={"username": "bob", "displayName": "Bob"},  # 无 password
        headers=AUTH_ADMIN,
    )
    assert resp.status_code == 422, resp.text
    assert any(
        "password" in str(e.get("loc", [])) for e in resp.json().get("detail", [])
    )


@pytest.mark.asyncio
async def test_admin_reset_password_works(
    client: AsyncClient, dbSession: AsyncSession
):
    """PUT /users/{id}/password → 204 → hash 更新 + must_change=True。"""
    create = await client.post(
        "/api/v1/users",
        json={
            "username": "carol",
            "displayName": "Carol",
            "password": "OldPass123",
        },
        headers=AUTH_ADMIN,
    )
    assert create.status_code == 201, create.text
    user_id = create.json()["id"]

    reset = await client.put(
        f"/api/v1/users/{user_id}/password",
        json={"newPassword": "NewPass456", "forceChangeOnNextLogin": True},
        headers=AUTH_ADMIN,
    )
    assert reset.status_code == 204, reset.text

    row = (
        await dbSession.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    assert bcrypt.checkpw(b"NewPass456", row.password_hash.encode())
    assert not bcrypt.checkpw(b"OldPass123", row.password_hash.encode())
    assert row.must_change_password is True


@pytest.mark.asyncio
async def test_admin_reset_password_weak_password_rejected(
    client: AsyncClient, dbSession: AsyncSession
):
    create = await client.post(
        "/api/v1/users",
        json={
            "username": "dave",
            "displayName": "Dave",
            "password": "ValidPass1",
        },
        headers=AUTH_ADMIN,
    )
    user_id = create.json()["id"]

    reset = await client.put(
        f"/api/v1/users/{user_id}/password",
        json={"newPassword": "short", "forceChangeOnNextLogin": False},
        headers=AUTH_ADMIN,
    )
    # "short" 缺数字 + <8 位 → validate_password 抛 ValidationError → 422
    assert reset.status_code in (400, 422), reset.text
