"""id_mapping 写入口 RBAC（A1 验收 #5：写入口为运维/后台专用）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。覆盖：
- 普通用户 POST / PUT / DELETE / 整表列举 → 403，且库内未被改动
- 普通用户按 unified_id 单点读 → 200（验收 #5 后半句：读路径不对业务用户关闭）
- admin 用户走 Bearer 可以正常写
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import Role, User, UserRole, UserSession
from app.services.jwt_codec import sign_jwt

MAPPINGS_URL = "/api/v1/id-mappings"


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _createUserWithRole(
    session: AsyncSession, username: str, role_code: str
) -> User:
    user = User(
        username=username,
        display_name=username.title(),
        email=None,
        enabled=True,
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
    user = await _createUserWithRole(dbSession, "acl_regular_user", "user")
    return await _mintToken(dbSession, user)


@pytest.fixture
async def adminToken(dbSession: AsyncSession) -> str:
    user = await _createUserWithRole(dbSession, "acl_admin_user", "admin")
    return await _mintToken(dbSession, user)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assertPermissionDenied(resp: Any) -> None:
    """403 且来自 requireAdmin（而非路径/契约类 403）。"""
    assert resp.status_code == 403, f"expected 403, got {resp.status_code}: {resp.text}"
    assert resp.json()["detail"]["code"] == "PERMISSION_DENIED", resp.text


async def _mappingCount(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT count(*) FROM id_mapping"))).scalar_one())


async def _seedMapping(session: AsyncSession, unifiedId: str, externalId: str) -> None:
    await session.execute(
        text("""
            INSERT INTO id_mapping (unified_id, business_object, external_id, pg_table, pg_id)
            VALUES (:uid, 'supplier', :ext, 'supplier', '1')
        """),
        {"uid": unifiedId, "ext": externalId},
    )
    await session.commit()


async def _mappingRow(session: AsyncSession, unifiedId: str) -> Any:
    row = await session.execute(
        text("SELECT unified_id, pg_table, pg_id FROM id_mapping WHERE unified_id = :uid"),
        {"uid": unifiedId},
    )
    return row.first()


@pytest.mark.asyncio
async def test_regular_user_cannot_create_mapping(
    client: AsyncClient, dbSession: AsyncSession, regularUserToken: str
) -> None:
    """普通用户 POST /api/v1/id-mappings → 403，且不落库。"""
    resp = await client.post(
        MAPPINGS_URL,
        json={"businessObject": "supplier", "externalId": "S900"},
        headers=_bearer(regularUserToken),
    )
    _assertPermissionDenied(resp)
    assert await _mappingCount(dbSession) == 0


@pytest.mark.asyncio
async def test_regular_user_cannot_update_mapping(
    client: AsyncClient, dbSession: AsyncSession, regularUserToken: str
) -> None:
    """普通用户 PUT /api/v1/id-mappings/{id} → 403，且库内 pg_id 不变。"""
    unifiedId = "obj:supplier:S901"
    await _seedMapping(dbSession, unifiedId, "S901")

    resp = await client.put(
        f"{MAPPINGS_URL}/{unifiedId}",
        json={"pgTable": "supplier", "pgId": "999"},
        headers=_bearer(regularUserToken),
    )
    _assertPermissionDenied(resp)

    row = await _mappingRow(dbSession, unifiedId)
    assert row is not None
    assert row.pg_id == "1", "普通用户不得改写 id_mapping"


@pytest.mark.asyncio
async def test_regular_user_cannot_delete_mapping(
    client: AsyncClient, dbSession: AsyncSession, regularUserToken: str
) -> None:
    """普通用户 DELETE /api/v1/id-mappings/{id} → 403，且行仍在。"""
    unifiedId = "obj:supplier:S902"
    await _seedMapping(dbSession, unifiedId, "S902")

    resp = await client.delete(
        f"{MAPPINGS_URL}/{unifiedId}", headers=_bearer(regularUserToken)
    )
    _assertPermissionDenied(resp)

    assert await _mappingRow(dbSession, unifiedId) is not None


@pytest.mark.asyncio
async def test_regular_user_cannot_list_all_mappings(
    client: AsyncClient, regularUserToken: str
) -> None:
    """普通用户 GET /api/v1/id-mappings（整表列举）→ 403。"""
    resp = await client.get(MAPPINGS_URL, headers=_bearer(regularUserToken))
    _assertPermissionDenied(resp)


@pytest.mark.asyncio
async def test_regular_user_can_read_single_mapping(
    client: AsyncClient, dbSession: AsyncSession, regularUserToken: str
) -> None:
    """普通用户 GET /api/v1/id-mappings/{id} → 200（读路径不关）。"""
    unifiedId = "obj:supplier:S903"
    await _seedMapping(dbSession, unifiedId, "S903")

    resp = await client.get(
        f"{MAPPINGS_URL}/{unifiedId}", headers=_bearer(regularUserToken)
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json()["unifiedId"] == unifiedId


@pytest.mark.asyncio
async def test_admin_can_create_and_delete_mapping(
    client: AsyncClient, adminToken: str
) -> None:
    """admin 走 Bearer：POST → 201，DELETE → 204（守卫不得误伤管理员）。"""
    create = await client.post(
        MAPPINGS_URL,
        json={"businessObject": "supplier", "externalId": "S904"},
        headers=_bearer(adminToken),
    )
    assert create.status_code == 201, create.text
    unifiedId = create.json()["unifiedId"]

    listing = await client.get(MAPPINGS_URL, headers=_bearer(adminToken))
    assert listing.status_code == 200, listing.text
    assert [m["unifiedId"] for m in listing.json()] == [unifiedId]

    delete = await client.delete(f"{MAPPINGS_URL}/{unifiedId}", headers=_bearer(adminToken))
    assert delete.status_code == 204, delete.text
