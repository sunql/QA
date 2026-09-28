"""getCurrentUser Bearer 路径集成测试（feat-user-auth）。

真实 PostgreSQL + 完整 SQLAlchemy 链路（按 Harness/rules/测试规范.md）。
覆盖：
- Bearer 命中有效 session → DB user + DB roles（忽略 stub 头）
- Bearer 但 session revoked → 403 MSG_TOKEN_REVOKED
- Bearer 但 session 过期（DB expires_at 早于 now）→ 403
- Bearer 但 user disabled → 403
- Bearer invalid token → 403
- stub 模式 X-User-Id 命中 DB → DB roles 覆盖 header
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.dependencies import getCurrentUser
from app.domain.exceptions import PermissionDeniedError
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
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


class TestBearerActiveSession:
    async def test_active_session_returns_db_user_and_db_roles(
        self, dbSession: AsyncSession
    ) -> None:
        user = await _create_user_with_role(
            dbSession, username="bearer_alice", role_code="analyst"
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

        current = await getCurrentUser(
            authorization=f"Bearer {token}",
            xUserId="someone_else",
            xTenantId=None,
            xUserRoles="admin",  # 应被忽略
            xUserDepartments=None,
            session=dbSession,
        )
        assert current.userId == "bearer_alice"
        assert current.roles == ("analyst",)  # DB 角色，非 header
        assert current.dbUserId == user.id
        assert current.jti == jti

    async def test_invalid_token_raises_403(
        self, dbSession: AsyncSession
    ) -> None:
        with pytest.raises(PermissionDeniedError):
            await getCurrentUser(
                authorization="Bearer not-a-jwt",
                xUserId=None,
                xTenantId=None,
                xUserRoles=None,
                xUserDepartments=None,
                session=dbSession,
            )


class TestBearerRevokedOrExpiredSession:
    async def test_revoked_session_raises_403(
        self, dbSession: AsyncSession
    ) -> None:
        user = await _create_user_with_role(
            dbSession, username="revoked_bob", role_code="user"
        )
        token, jti, exp = sign_jwt(user_id=user.id, username=user.username)
        dbSession.add(
            UserSession(
                jti=jti,
                user_id=user.id,
                issued_at=datetime.now(timezone.utc),
                expires_at=exp,
                revoked_at=datetime.now(timezone.utc),
                revoked_reason="logout",
            )
        )
        await dbSession.commit()

        with pytest.raises(PermissionDeniedError) as exc_info:
            await getCurrentUser(
                authorization=f"Bearer {token}",
                xUserId=None,
                xTenantId=None,
                xUserRoles=None,
                xUserDepartments=None,
                session=dbSession,
            )
        assert "失效" in exc_info.value.message or "吊销" in exc_info.value.message

    async def test_expired_db_session_raises_403(
        self, dbSession: AsyncSession
    ) -> None:
        """user_sessions.expires_at 已过 → 403（即便 JWT 本身未过期）。"""
        user = await _create_user_with_role(
            dbSession, username="expired_carol", role_code="user"
        )
        token, jti, _ = sign_jwt(user_id=user.id, username=user.username)
        # 模拟 DB 里 expires_at 早于 now
        dbSession.add(
            UserSession(
                jti=jti,
                user_id=user.id,
                issued_at=datetime.now(timezone.utc) - timedelta(hours=2),
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
        await dbSession.commit()

        with pytest.raises(PermissionDeniedError):
            await getCurrentUser(
                authorization=f"Bearer {token}",
                xUserId=None,
                xTenantId=None,
                xUserRoles=None,
                xUserDepartments=None,
                session=dbSession,
            )


class TestBearerDisabledUser:
    async def test_disabled_user_raises_403(
        self, dbSession: AsyncSession
    ) -> None:
        user = await _create_user_with_role(
            dbSession, username="disabled_dave", role_code="user", enabled=False
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

        with pytest.raises(PermissionDeniedError) as exc_info:
            await getCurrentUser(
                authorization=f"Bearer {token}",
                xUserId=None,
                xTenantId=None,
                xUserRoles=None,
                xUserDepartments=None,
                session=dbSession,
            )
        assert exc_info.value.message == "请先登录"


class TestStubModeDbEnrichment:
    async def test_x_user_id_hit_db_returns_db_roles(
        self, dbSession: AsyncSession
    ) -> None:
        """stub 模式 X-User-Id 命中 DB → DB roles 覆盖 header。"""
        user = await _create_user_with_role(
            dbSession, username="stub_eve", role_code="manager"
        )
        current = await getCurrentUser(
            authorization=None,
            xUserId="stub_eve",
            xTenantId=None,
            xUserRoles="admin",  # 应被忽略
            xUserDepartments=None,
            session=dbSession,
        )
        assert current.userId == "stub_eve"
        assert current.roles == ("manager",)
        assert current.dbUserId == user.id