"""认证 5 个审计事件集成测试（feat-user-auth）。

真实 PG + audit_log 表断言。
"""

from __future__ import annotations

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import getSettings
from app.domain.models import AuditLog
from app.models.rbac import User, UserSession
from app.services.jwt_codec import sign_jwt


def _hash(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _seed_user_with_password(
    dbSession, *, username: str, password: str = "Abcd1234"
) -> User:
    user = User(
        username=username,
        display_name=username.title(),
        email=None,
        enabled=True,
        password_hash=_hash(password),
    )
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


ADMIN_AUTH = {
    "X-User-Id": "admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}


class TestAuthAuditEvents:
    async def test_login_success_writes_auth_login(
        self, client: AsyncClient, dbSession
    ) -> None:
        await _seed_user_with_password(dbSession, username="audit_login_ok")
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "audit_login_ok", "password": "Abcd1234"},
        )
        assert r.status_code == 200

        rows = (await dbSession.execute(
            select(AuditLog).where(AuditLog.action == "auth.login")
        )).scalars().all()
        assert len(rows) >= 1
        assert rows[-1].entity_type == "user"
        assert rows[-1].actor == "audit_login_ok"

    async def test_login_failed_writes_auth_login_failed(
        self, client: AsyncClient, dbSession
    ) -> None:
        await _seed_user_with_password(dbSession, username="audit_login_fail")
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "audit_login_fail", "password": "WrongPwd1"},
        )
        assert r.status_code == 401

        rows = (await dbSession.execute(
            select(AuditLog).where(AuditLog.action == "auth.login_failed")
        )).scalars().all()
        assert len(rows) >= 1
        assert rows[-1].actor == "audit_login_fail"

    async def test_logout_writes_auth_logout(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="audit_logout")
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
        await dbSession.commit()

        r = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 204

        rows = (await dbSession.execute(
            select(AuditLog).where(AuditLog.action == "auth.logout")
        )).scalars().all()
        assert len(rows) >= 1

    async def test_change_password_writes_auth_password_changed(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="audit_chgpwd")
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
        await dbSession.commit()

        r = await client.put(
            "/api/v1/auth/me/password",
            headers={"Authorization": f"Bearer {token}"},
            json={"oldPassword": "Abcd1234", "newPassword": "NewPwd4567"},
        )
        assert r.status_code == 204

        rows = (await dbSession.execute(
            select(AuditLog).where(AuditLog.action == "auth.password_changed")
        )).scalars().all()
        assert len(rows) >= 1

    async def test_admin_reset_writes_user_password_reset(
        self, client: AsyncClient, dbSession
    ) -> None:
        target = await _seed_user_with_password(dbSession, username="audit_reset")
        r = await client.put(
            f"/api/v1/users/{target.id}/password",
            headers=ADMIN_AUTH,
            json={"newPassword": "ResetPwd1", "forceChangeOnNextLogin": True},
        )
        assert r.status_code == 204

        rows = (await dbSession.execute(
            select(AuditLog).where(AuditLog.action == "user.password_reset")
        )).scalars().all()
        assert len(rows) >= 1
        assert rows[-1].actor == "admin"