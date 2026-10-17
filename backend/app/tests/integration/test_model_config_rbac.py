"""model-config RBAC + endpoint-change + service-layer 防御纵深集成测试。

真实 PostgreSQL + 完整 API 链路（按 Harness/rules/测试规范.md）。
覆盖：
- 普通用户 POST /api/v1/models → 403
- admin 用户 POST /api/v1/models → 201
- admin 用户 PUT 变更 apiEndpoint 但不带 apiKey → 422
- X1 防御纵深：service 层 create/update/deactivate 在 actor 非 admin 时抛 PermissionDenied
- X1 缓存失效：apiEndpoint 变更后客户端缓存被清（下次 getOrCreateClient 重新构造）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import PermissionDeniedError
from app.domain.models import LlmConfig
from app.domain.schemas import LlmConfigCreate, LlmConfigUpdate
from app.infrastructure.llm import factory as llmFactory
from app.models.rbac import Role, User, UserRole, UserSession
from app.infrastructure.security.crypto import encryptApiKey
from app.services.jwt_codec import sign_jwt
from app.services.model_config_service import ModelConfigService


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


# ---------------------------------------------------------------------------
# X1 防御纵深：service 层 create/update/deactivate 接受 actor: CurrentUser，
# 非 admin 直接抛 PermissionDenied（即使绕过 API 路由也无法落库）。
# ---------------------------------------------------------------------------


async def _currentUserFor(
    session: AsyncSession, username: str, role_code: str
) -> CurrentUser:
    """造一个内存 CurrentUser（不签 JWT，直接走 service 层）。"""
    user = await _createUserWithRole(session, username, role_code=role_code)
    return CurrentUser(
        userId=str(user.id),
        roles=(role_code,),
        dbUserId=user.id,
    )


@pytest.mark.asyncio
async def test_serviceLayerRejectsNonAdminCreate(dbSession: AsyncSession) -> None:
    """X1：非 admin 直接调 service.create → PermissionDenied（防御纵深）。

    即使绕开 API 路由层 Depends(requireAdmin) 直接调 service，也无法越权。
    """
    actor = await _currentUserFor(dbSession, "x1_svc_create_user", "user")
    svc = ModelConfigService()
    dto = LlmConfigCreate(
        model_name="x1-svc-create-blocked",
        provider="openai",
        api_endpoint="https://api.openai.com/v1",
        api_key="sk-test",
    )
    with pytest.raises(PermissionDeniedError):
        await svc.create(dbSession, dto, actor=actor)


@pytest.mark.asyncio
async def test_serviceLayerAllowsAdminCreate(dbSession: AsyncSession) -> None:
    """X1：admin 直接调 service.create → 成功。"""
    actor = await _currentUserFor(dbSession, "x1_svc_create_admin", "admin")
    svc = ModelConfigService()
    dto = LlmConfigCreate(
        model_name="x1-svc-create-allowed",
        provider="openai",
        api_endpoint="https://api.openai.com/v1",
        api_key="sk-test",
    )
    config = await svc.create(dbSession, dto, actor=actor)
    assert config.id is not None
    assert config.model_name == "x1-svc-create-allowed"


@pytest.mark.asyncio
async def test_serviceLayerRejectsNonAdminUpdate(
    dbSession: AsyncSession, existingModelConfig: LlmConfig
) -> None:
    """X1：非 admin 直接调 service.update → PermissionDenied。"""
    actor = await _currentUserFor(dbSession, "x1_svc_update_user", "user")
    svc = ModelConfigService()
    dto = LlmConfigUpdate(weight=42)
    with pytest.raises(PermissionDeniedError):
        await svc.update(dbSession, existingModelConfig.id, dto, actor=actor)


@pytest.mark.asyncio
async def test_serviceLayerRejectsNonAdminDeactivate(
    dbSession: AsyncSession, existingModelConfig: LlmConfig
) -> None:
    """X1：非 admin 直接调 service.deactivate → PermissionDenied。"""
    actor = await _currentUserFor(dbSession, "x1_svc_deact_user", "user")
    svc = ModelConfigService()
    with pytest.raises(PermissionDeniedError):
        await svc.deactivate(dbSession, existingModelConfig.id, actor=actor)


@pytest.mark.asyncio
async def test_endpointChangeInvalidatesClientCache(
    dbSession: AsyncSession, existingModelConfig: LlmConfig
) -> None:
    """X1 cache：apiEndpoint 变更后客户端缓存被清（下次 getOrCreateClient 重读）。

    验证生产 2026-10-03 事故根因的修复：旧客户端若不清缓存，进程永远用旧 endpoint。
    getOrCreateClient 走 _buildClient LRU（cache key 含 updated_at_epoch/endpoint/key_cipher），
    update 后必须 clear 整个 LRU，否则旧 key 永远命中。
    """
    actor = await _currentUserFor(dbSession, "x1_cache_admin", "admin")
    svc = ModelConfigService()

    # 预热：构造一次客户端，确保 _buildClient LRU 缓存有 id 条目
    pre = await llmFactory.getOrCreateClient(existingModelConfig.id, dbSession)
    assert pre is not None
    infoBefore = llmFactory._buildClient.cache_info()
    assert infoBefore.currsize >= 1, "预热后 LRU 应有缓存条目"

    # 改 endpoint（必须同时给 key；否则会因 endpoint 变更缺 key 抛 422）
    await svc.update(
        dbSession,
        existingModelConfig.id,
        LlmConfigUpdate(
            api_endpoint="https://api.anthropic.com/v1",
            api_key="sk-new-key-after-endpoint-change",
        ),
        actor=actor,
    )

    # 验证：LRU 缓存被 clear（旧 key 不会命中）
    infoAfter = llmFactory._buildClient.cache_info()
    assert infoAfter.currsize == 0, (
        f"update 后 LRU 缓存应被 clear，实际 currsize={infoAfter.currsize}"
    )

    # 验证：再 getOrCreateClient 拿到的是新对象（endpont/key 都已换）
    newClient = await llmFactory.getOrCreateClient(existingModelConfig.id, dbSession)
    assert newClient is not None
    # OpenAiClient 在构造时固化 _apiEndpoint（避免 ORM 跨会话 DetachedInstanceError）
    newEndpoint = getattr(newClient, "_apiEndpoint", None)
    assert newEndpoint == "https://api.anthropic.com/v1", (
        f"新客户端应绑新 endpoint，实际 {newEndpoint!r}"
    )
