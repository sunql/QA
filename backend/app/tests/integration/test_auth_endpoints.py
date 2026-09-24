"""认证 5 端点集成测试（feat-user-auth）。

真实 PG + 完整 API 链路。
覆盖：
- POST /auth/login 成功 → 200 + token + UserSummaryRead
- POST /auth/login 错密码 → 401 + MSG_INVALID_CREDENTIALS
- POST /auth/login 不存在用户 → 401（防枚举）
- POST /auth/login 空字段 → 422
- GET /auth/me + Bearer → 200
- GET /auth/me 无 Bearer → 403
- POST /auth/logout + Bearer → 204 + session revoked_at 写入
- PUT /auth/me/password + Bearer → 204 + 所有 session 吊销
- GET /auth/password-policy 公开 → 200
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
    dbSession, *, username: str = "login_user", password: str = "Abcd1234"
) -> User:
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


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


class TestLogin:
    async def test_login_success_returns_token_and_user(
        self, client: AsyncClient, dbSession
    ) -> None:
        await _seed_user_with_password(dbSession, username="login_alice", password="Abcd1234")
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "login_alice", "password": "Abcd1234"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["tokenType"] == "Bearer"
        assert body["expiresIn"] == getSettings().jwtTtlSeconds
        assert body["mustChangePassword"] is True
        assert "accessToken" in body
        assert body["user"]["username"] == "login_alice"
        assert "password" not in body["user"]
        assert "passwordHash" not in body["user"]

    async def test_login_wrong_password_returns_401(
        self, client: AsyncClient, dbSession
    ) -> None:
        await _seed_user_with_password(dbSession, username="login_bob", password="Abcd1234")
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "login_bob", "password": "WrongPwd1"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "用户名或密码错误"

    async def test_login_unknown_user_returns_401(
        self, client: AsyncClient
    ) -> None:
        """查无此人 + 等长延迟防时间侧信道。"""
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "nobody_user", "password": "AnyPwd1"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "用户名或密码错误"

    async def test_login_disabled_user_returns_401(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="login_dave")
        user.enabled = False
        await dbSession.commit()
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "login_dave", "password": "Abcd1234"},
        )
        assert r.status_code == 401

    async def test_login_no_password_returns_401(
        self, client: AsyncClient, dbSession
    ) -> None:
        """用户 password_hash 为空（旧 user）→ 401。"""
        user = User(
            username="no_pwd_user",
            display_name="No Pwd",
            email=None,
            enabled=True,
            password_hash=None,
        )
        dbSession.add(user)
        await dbSession.commit()
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "no_pwd_user", "password": "AnyPwd1"},
        )
        assert r.status_code == 401

    async def test_login_empty_fields_returns_422(
        self, client: AsyncClient
    ) -> None:
        r = await client.post(
            "/api/v1/auth/login", json={"username": "", "password": ""}
        )
        assert r.status_code == 422

    async def test_login_creates_user_session(
        self, client: AsyncClient, dbSession
    ) -> None:
        await _seed_user_with_password(dbSession, username="session_user")
        r = await client.post(
            "/api/v1/auth/login",
            json={"username": "session_user", "password": "Abcd1234"},
        )
        assert r.status_code == 200
        rows = (await dbSession.execute(select(UserSession))).scalars().all()
        assert len(rows) == 1
        assert rows[0].revoked_at is None


class TestMe:
    async def test_me_with_bearer_returns_user(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="me_user")
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

        r = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "me_user"
        assert "password" not in body
        assert "passwordHash" not in body

    async def test_me_without_bearer_returns_401(
        self, client: AsyncClient
    ) -> None:
        """无 Bearer + stub 模式 → anonymous → AuthMeRead 要求 dbUserId → 401。"""
        r = await client.get("/api/v1/auth/me")
        assert r.status_code == 401
        assert r.json()["error"] == "请先登录"


class TestLogout:
    async def test_logout_revokes_session(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="logout_user")
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

        rows = (await dbSession.execute(select(UserSession))).scalars().all()
        assert all(s.revoked_at is not None for s in rows)
        assert any(s.revoked_reason == "logout" for s in rows)


class TestChangePassword:
    async def test_change_own_password_revokes_all_sessions(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="changepwd_user")
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

        rows = (await dbSession.execute(select(UserSession))).scalars().all()
        assert all(s.revoked_at is not None for s in rows)
        assert any(s.revoked_reason == "password_changed" for s in rows)

    async def test_change_own_password_wrong_old_returns_401(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="changepwd_wrong")
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
            json={"oldPassword": "WrongOld1", "newPassword": "NewPwd4567"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "当前密码不正确"

    async def test_change_own_password_weak_returns_422(
        self, client: AsyncClient, dbSession
    ) -> None:
        user = await _seed_user_with_password(dbSession, username="changepwd_weak")
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
            json={"oldPassword": "Abcd1234", "newPassword": "weak"},
        )
        assert r.status_code == 422


class TestPasswordPolicy:
    async def test_password_policy_is_public(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/auth/password-policy")
        assert r.status_code == 200
        body = r.json()
        assert body["minLength"] == 8
        assert body["requireLetter"] is True
        assert body["requireDigit"] is True