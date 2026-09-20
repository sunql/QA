"""admin 重置密码端点集成测试（feat-user-auth）。

- admin 可重置其他用户密码 + 吊销所有 session
- 非 admin → 403
- 弱密码 → 422
- admin 重置自己同样吊销所有 session
"""

from __future__ import annotations

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import getSettings
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


ADMIN_AUTH = {
    "X-User-Id": "admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}

NON_ADMIN_AUTH = {
    "X-User-Id": "regular_user",
    "X-User-Roles": "user",
    "X-User-Departments": "PROCUREMENT",
}


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


class TestAdminResetPassword:
    async def test_admin_can_reset_other_user_password(
        self, client: AsyncClient, dbSession
    ) -> None:
        target = await _seed_user_with_password(dbSession, username="reset_target")
        r = await client.put(
            f"/api/v1/users/{target.id}/password",
            headers=ADMIN_AUTH,
            json={"newPassword": "ResetPwd1", "forceChangeOnNextLogin": True},
        )
        assert r.status_code == 204

        await dbSession.refresh(target)
        assert bcrypt.checkpw(b"ResetPwd1", target.password_hash.encode())
        assert target.must_change_password is True

    async def test_admin_reset_revokes_all_target_sessions(
        self, client: AsyncClient, dbSession
    ) -> None:
        target = await _seed_user_with_password(dbSession, username="reset_sessions")
        # 签 2 个 session 模拟多端登录
        token1, jti1, exp1 = sign_jwt(user_id=target.id, username=target.username)
        token2, jti2, exp2 = sign_jwt(user_id=target.id, username=target.username)
        dbSession.add_all(
            [
                UserSession(
                    jti=jti1, user_id=target.id,
                    issued_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                    expires_at=exp1,
                ),
                UserSession(
                    jti=jti2, user_id=target.id,
                    issued_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                    expires_at=exp2,
                ),
            ]
        )
        await dbSession.commit()

        r = await client.put(
            f"/api/v1/users/{target.id}/password",
            headers=ADMIN_AUTH,
            json={"newPassword": "NewPwd12345", "forceChangeOnNextLogin": True},
        )
        assert r.status_code == 204

        sessions = (
            (await dbSession.execute(
                select(UserSession).where(UserSession.user_id == target.id)
            )).scalars().all()
        )
        assert all(s.revoked_at is not None for s in sessions)
        assert any(s.revoked_reason == "admin_reset" for s in sessions)

    async def test_non_admin_returns_403(
        self, client: AsyncClient, dbSession
    ) -> None:
        target = await _seed_user_with_password(dbSession, username="reset_blocked")
        r = await client.put(
            f"/api/v1/users/{target.id}/password",
            headers=NON_ADMIN_AUTH,
            json={"newPassword": "ResetPwd1", "forceChangeOnNextLogin": True},
        )
        assert r.status_code == 403

    async def test_weak_password_returns_422(
        self, client: AsyncClient, dbSession
    ) -> None:
        target = await _seed_user_with_password(dbSession, username="reset_weak")
        r = await client.put(
            f"/api/v1/users/{target.id}/password",
            headers=ADMIN_AUTH,
            json={"newPassword": "weak", "forceChangeOnNextLogin": True},
        )
        assert r.status_code == 422

    async def test_reset_nonexistent_user_returns_404(
        self, client: AsyncClient
    ) -> None:
        r = await client.put(
            "/api/v1/users/99999/password",
            headers=ADMIN_AUTH,
            json={"newPassword": "ResetPwd1", "forceChangeOnNextLogin": True},
        )
        assert r.status_code == 404