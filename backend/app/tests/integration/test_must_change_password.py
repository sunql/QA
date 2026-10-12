"""must_change_password 服务端强制集成测试（audit-fx-1.3）。

覆盖：
- must_change_password=true 用户访问业务端点 → 403 + detail 含 must_change_password
- must_change_password=true 用户可访问 /auth/me
- must_change_password=true 用户可访问 /auth/change-password
- 改密后 guard 解除，业务端点不再被阻断
"""

from __future__ import annotations

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import Role, User, UserRole, UserSession
from app.services.jwt_codec import sign_jwt


def _hash(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _seed_must_change_user(
    dbSession: AsyncSession, *, username: str = "must_change_u", password: str = "OldPwd1234"
) -> User:
    """造一个 must_change_password=True 的用户。"""
    user = User(
        username=username,
        display_name=username.title(),
        email=None,
        enabled=True,
        password_hash=_hash(password),
        must_change_password=True,
    )
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)
    return user


def _make_session(dbSession: AsyncSession, user: User) -> tuple[str, str, int]:
    """为 user 签发 JWT 并写入 user_sessions。"""
    token, jti, exp = sign_jwt(user_id=user.id, username=user.username)
    dbSession.add(
        UserSession(
            jti=jti,
            user_id=user.id,
            issued_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            expires_at=exp,
        )
    )
    return token, jti, exp


@pytest.mark.asyncio
async def test_must_change_user_blocked_from_business(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """must_change_password=true 用户访问 POST /api/v1/models → 403。"""
    user = await _seed_must_change_user(dbSession, username="biz_blocked_u")
    token, _, _ = _make_session(dbSession, user)
    await dbSession.commit()

    # must_change user tries to access a business endpoint → 403
    resp = await client.post(
        "/api/v1/models",
        headers={"Authorization": f"Bearer {token}"},
        json={"model_name": "EvilModel", "provider": "openai"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail.get("code") == "MUST_CHANGE_PASSWORD"


@pytest.mark.asyncio
async def test_must_change_user_can_change_password(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """must_change_password=true 用户可调用 PUT /auth/me/password。"""
    user = await _seed_must_change_user(dbSession, username="can_change_u")
    token, _, _ = _make_session(dbSession, user)
    await dbSession.commit()

    resp = await client.put(
        "/api/v1/auth/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"oldPassword": "OldPwd1234", "newPassword": "NewPwd5678"},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_must_change_user_can_call_me(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """must_change_password=true 用户可调用 GET /auth/me。"""
    user = await _seed_must_change_user(dbSession, username="can_me_u")
    token, _, _ = _make_session(dbSession, user)
    await dbSession.commit()

    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "can_me_u"
    assert body["mustChangePassword"] is True


@pytest.mark.asyncio
async def test_after_change_password_guard_removed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """改密后 must_change_password=false，业务端点不再被阻断（RBAC 仍生效）。"""
    # Create must_change user with admin role so RBAC won't block after guard removal
    user = User(
        username="after_change_admin",
        display_name="After Change Admin",
        email=None,
        enabled=True,
        password_hash=_hash("OldPwd1234"),
        must_change_password=True,
    )
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)

    # Assign admin role (seed if not present)
    admin_role = (
        await dbSession.execute(select(Role).where(Role.code == "admin"))
    ).scalar_one_or_none()
    if admin_role is None:
        admin_role = Role(code="admin", name="Admin")
        dbSession.add(admin_role)
        await dbSession.commit()
        await dbSession.refresh(admin_role)
    dbSession.add(UserRole(user_id=user.id, role_id=admin_role.id))
    await dbSession.commit()

    token, _, _ = _make_session(dbSession, user)
    await dbSession.commit()

    # Change password (old session gets revoked)
    resp = await client.put(
        "/api/v1/auth/me/password",
        headers={"Authorization": f"Bearer {token}"},
        json={"oldPassword": "OldPwd1234", "newPassword": "NewPwd5678"},
    )
    assert resp.status_code == 204

    # Re-issue a new valid session (old one was revoked by change_own_password)
    new_token, new_jti, new_exp = sign_jwt(user_id=user.id, username=user.username)
    dbSession.add(
        UserSession(
            jti=new_jti,
            user_id=user.id,
            issued_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            expires_at=new_exp,
        )
    )
    await dbSession.commit()

    # Now the user (as admin) can create model-configs → 201
    resp = await client.post(
        "/api/v1/models",
        headers={"Authorization": f"Bearer {new_token}"},
        json={"model_name": "AfterChangeModel", "provider": "openai"},
    )
    assert resp.status_code == 201
