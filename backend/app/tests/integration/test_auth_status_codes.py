"""401 vs 403 拆分集成测试（audit-fx-2.1 Finding 4）。

真实 PG + 完整 API 链路。覆盖：
- 过期 / 无效 / 缺失 Bearer token → 401 + detail.code == "AUTH_EXPIRED" 等
- 已认证但无 admin 角色访问管理端点 → 403
- enabled=False 用户带有效 token → 401 AUTH_DISABLED（fix round 1）

Fixtures:
- expiredToken: 已过期的 JWT（JWT 本身过期，但 session 记录可能仍存在）
- userWithoutAdminToken: 拥有有效 session 但只有 user 角色的用户的 token
- disabledUserToken: enabled=False 用户带有效 session
"""

from __future__ import annotations

import bcrypt
from datetime import datetime, timedelta, timezone
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import Role, User, UserRole, UserSession
from app.services.jwt_codec import sign_jwt


def _hash(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _create_user_with_role(
    session: AsyncSession,
    username: str,
    role_code: str = "user",
    enabled: bool = True,
) -> User:
    user = User(
        username=username,
        display_name=username.title(),
        email=None,
        enabled=enabled,
        password_hash=_hash("Abcd1234"),
    )
    session.add(user)
    await session.flush()

    role = (
        await session.execute(select(Role).where(Role.code == role_code))
    ).scalar_one_or_none()
    if role is None:
        role = Role(code=role_code, name=role_code, description="test")
        session.add(role)
        await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    await session.commit()
    await session.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import getSettings

    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


@pytest.fixture
async def expiredToken(dbSession: AsyncSession) -> str:
    """返回已过期的 JWT token（JWT exp 已过，signature 有效）。"""
    user = await _create_user_with_role(dbSession, username="expired_token_user")
    # 手动签发一个已过期的 token（JWT exp 已过，signature 用真实 jwtSecret）
    import uuid

    import jwt as pyjwt
    from app.config import getSettings

    settings = getSettings()
    now = datetime.now(timezone.utc)
    expired_exp = now - timedelta(hours=1)  # 已过期 1 小时
    jti = str(uuid.uuid4())
    payload = {
        "iss": settings.jwtIssuer,
        "aud": settings.jwtAudience,
        "sub": str(user.id),
        "username": user.username,
        "jti": jti,
        "iat": int((now - timedelta(hours=2)).timestamp()),
        "exp": int(expired_exp.timestamp()),
    }
    token = pyjwt.encode(payload, settings.jwtSecret, algorithm=settings.jwtAlgorithm)
    return token


@pytest.fixture
async def userWithoutAdminToken(dbSession: AsyncSession) -> tuple[str, User]:
    """返回拥有有效 session 但只有 user 角色（无 admin）的用户 token。"""
    user = await _create_user_with_role(dbSession, username="non_admin_user", role_code="user")
    token, jti, exp = sign_jwt(user_id=user.id, username=user.username)
    dbSession.add(
        UserSession(
            jti=jti,
            user_id=user.id,
            issued_at=datetime.now(timezone.utc),
            expires_at=exp,
        )
    )
    await dbSession.commit()
    return token, user


@pytest.fixture
async def disabledUserToken(dbSession: AsyncSession) -> tuple[str, User]:
    """返回 enabled=False 用户带有效 session 的 token（fix round 1 回归测试）。"""
    user = await _create_user_with_role(
        dbSession, username="disabled_auth_user", role_code="user", enabled=False
    )
    token, jti, exp = sign_jwt(user_id=user.id, username=user.username)
    dbSession.add(
        UserSession(
            jti=jti,
            user_id=user.id,
            issued_at=datetime.now(timezone.utc),
            expires_at=exp,
        )
    )
    await dbSession.commit()
    return token, user


class TestAuth401:
    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self, client: AsyncClient, expiredToken: str) -> None:
        """过期的 JWT → 401 + AUTH_EXPIRED。"""
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expiredToken}"})
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["code"] == "AUTH_EXPIRED"

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self, client: AsyncClient) -> None:
        """无效签名 / 格式错误的 JWT → 401。"""
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer bad.token.here"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_disabled_user_returns_401(self, client: AsyncClient, disabledUserToken: tuple[str, User]) -> None:
        """enabled=False 用户带有效 token → 401 AUTH_DISABLED（fix round 1）。"""
        token, _ = disabledUserToken
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["code"] == "AUTH_DISABLED"


class TestAuth403:
    @pytest.mark.asyncio
    async def test_logged_in_but_no_permission_returns_403(
        self, client: AsyncClient, userWithoutAdminToken: tuple[str, User]
    ) -> None:
        """已认证（有效 token）但无 admin 角色访问管理端点 → 403。"""
        token, _ = userWithoutAdminToken
        resp = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
