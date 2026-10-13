"""model-config RBAC + endpoint-change 校验集成测试。

真实 PostgreSQL + 完整 API 链路（按 Harness/rules/测试规范.md）。
覆盖：
- 普通用户 POST /api/v1/models → 403
- admin 用户 POST /api/v1/models → 201
- admin 用户 PUT 变更 apiEndpoint 但不带 apiKey → 422
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.models.rbac import Role, User, UserRole, UserSession
from app.infrastructure.security.crypto import encryptApiKey
from app.services.jwt_codec import sign_jwt


def _hash(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _createUserWithRole(
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


async def _mintToken(session: AsyncSession, user: User) -> str:
    token, jti, exp = sign_jwt(user_id=user.id, username=user.username)
    session.add(
        UserSession(
            jti=jti,
            user_id=user.id,
            issued_at=datetime.now(timezone.utc),
            expires_at=exp,
        )
    )
    await session.commit()
    return token


@pytest.fixture
async def regularUserToken(dbSession: AsyncSession) -> str:
    user = await _createUserWithRole(dbSession, "test_regular_rbac", role_code="user")
    return await _mintToken(dbSession, user)


@pytest.fixture
async def adminToken(dbSession: AsyncSession) -> str:
    user = await _createUserWithRole(dbSession, "test_admin_rbac", role_code="admin")
    return await _mintToken(dbSession, user)


@pytest.fixture
async def existingModelConfig(dbSession: AsyncSession) -> LlmConfig:
    config = LlmConfig(
        model_name="test_model_config_rbac",
        provider="openai",
        api_endpoint="https://api.openai.com/v1",
        api_key_encrypted=encryptApiKey("sk-old-key"),
        cost_per_1k_input=0,
        cost_per_1k_output=0,
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


@pytest.mark.asyncio
async def test_unprivilegedUserCannotCreateModel(
    client: AsyncClient, regularUserToken: str
) -> None:
    """普通用户 POST /api/v1/models → 403。"""
    headers = {"Authorization": f"Bearer {regularUserToken}"}
    resp = await client.post(
        "/api/v1/models",
        json={
            "modelName": "new-test-model",
            "provider": "openai",
            "apiEndpoint": "https://api.openai.com/v1",
            "apiKey": "sk-test",
        },
        headers=headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_adminCanCreateModel(client: AsyncClient, adminToken: str) -> None:
    """admin 用户 POST /api/v1/models → 201。"""
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.post(
        "/api/v1/models",
        json={
            "modelName": "test-admin-create-rbac",
            "provider": "openai",
            "apiEndpoint": "https://api.openai.com/v1",
            "apiKey": "sk-test",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["modelName"] == "test-admin-create-rbac"


@pytest.mark.asyncio
async def test_adminCreateModelWithHttpEndpointBlocked(
    client: AsyncClient, adminToken: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """admin 在生产环境 POST 带 http:// endpoint → 422（URL 校验失败）。"""
    monkeypatch.setenv("APP_ENV", "production")
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.post(
        "/api/v1/models",
        json={
            "modelName": "test-http-blocked",
            "provider": "openai",
            "apiEndpoint": "http://api.openai.com/v1",
            "apiKey": "sk-test",
        },
        headers=headers,
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "https" in (body.get("error") or body.get("detail") or "")


@pytest.mark.asyncio
async def test_endpointChangeRequiresNewKey(
    client: AsyncClient, adminToken: str, existingModelConfig: LlmConfig
) -> None:
    """变更 apiEndpoint 时不带 apiKey → 422（提示缺少 apiKey）。"""
    headers = {"Authorization": f"Bearer {adminToken}"}
    resp = await client.put(
        f"/api/v1/models/{existingModelConfig.id}",
        json={"apiEndpoint": "https://api.anthropic.com/v1"},
        headers=headers,
    )
    assert resp.status_code == 422
    # DomainError handler returns {"error": <message>, "detail": <detail>, ...}
    body = resp.json()
    assert "apiKey" in (body.get("error") or body.get("detail") or "")
