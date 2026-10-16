"""User restricted_access 字段 + admin PATCH 端点集成测试（Task 4.2：audit-fx-4.2）。

TDD RED：先写测试，验证 field 不存在 → 实现 → GREEN。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import User
from app.models.rbac import ADMIN_ROLE_CODE

AUTH_ADMIN = {
    "X-User-Id": "admin",
    "X-User-Roles": ADMIN_ROLE_CODE,
    "X-User-Departments": "IT",
}


@pytest.fixture(autouse=True)
async def _rbacSeeds(client: AsyncClient, dbSession: AsyncSession) -> None:
    """幂等 seed RBAC 基线（admin 用户/角色）+ 菜单行。

    同时清除 admin 的 must_change_password 标志，避免触发 MUST_CHANGE_PASSWORD 守卫
    导致所有 API 请求返回 403。
    """
    from scripts.seed_rbac import seedRbacBaseline
    from app.domain.models import User
    from app.models.menu_config import MenuConfig

    await seedRbacBaseline(dbSession)

    # 清除 admin 的 must_change_password 标志（seed 默认 True）
    admin_user = await dbSession.execute(
        select(User).where(User.username == "admin")
    )
    admin = admin_user.scalar_one_or_none()
    if admin and admin.must_change_password:
        admin.must_change_password = False
        await dbSession.commit()

    # 同步 seed menus（item.profile 等菜单, IdentityService.create_user 会写 permission_grant）
    # 71 红 A1 流 M1 修（fix/test-fixture-item-profile-seed）：item.profile 顶层插入
    # 会被 MenuConfigService 当 section 触发 MenuConfigStructureError（list_sections
    # 「section 必须 path=None」校验）。与 test_rbac_api._seedMenus /
    # test_identity_create_user_password 同款 child leaf 模式对齐。
    section = MenuConfig(code="adminsys", label_key="admin_sys", sort_order=90)
    section.children = [
        MenuConfig(code=code, label_key=code, path=f"/admin/{code}", sort_order=i)
        for i, code in enumerate(["menu_a", "menu_b", "menu_c", "menu_d"])
    ] + [
        MenuConfig(
            code="item.profile", label_key="profile", path="/profile", sort_order=99
        ),
    ]
    dbSession.add(section)
    await dbSession.commit()


@pytest.mark.asyncio
async def test_user_has_restricted_access_field_default_false(
    dbSession: AsyncSession,
    client: AsyncClient,
) -> None:
    """新创建的用户 restricted_access 字段默认为 False（ORM 层面断言）。"""
    # 建一个新用户
    r = await client.post(
        "/api/v1/users",
        headers=AUTH_ADMIN,
        json={
            "username": "test_restricted_user",
            "display_name": "Test Restricted User",
            "email": None,
            "password": "TestPass123!",
        },
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    # 从 DB 直接读取，验证 field 存在且默认 False
    user_row = await dbSession.get(User, user_id)
    assert hasattr(user_row, "restricted_access"), (
        "User model must have restricted_access field"
    )
    assert user_row.restricted_access is False, (
        "restricted_access must default to False"
    )


@pytest.mark.asyncio
async def test_admin_can_grant_restricted_access(
    dbSession: AsyncSession,
    client: AsyncClient,
) -> None:
    """admin PATCH /api/v1/users/{id} 设置 restrictedAccess=true，返回 200 且字段生效。"""
    # 建一个新用户
    r = await client.post(
        "/api/v1/users",
        headers=AUTH_ADMIN,
        json={
            "username": "test_ra_target",
            "display_name": "Test RA Target",
            "email": None,
            "password": "TestPass123!",
        },
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    # admin PATCH 赋予 restricted_access
    r2 = await client.patch(
        f"/api/v1/users/{user_id}",
        headers=AUTH_ADMIN,
        json={"restrictedAccess": True},
    )
    assert r2.status_code == 200, r2.text

    # 验证字段已更新
    user_row = await dbSession.get(User, user_id)
    assert user_row.restricted_access is True, (
        "restricted_access should be True after admin patch"
    )


@pytest.mark.asyncio
async def test_admin_can_clear_restricted_access(
    dbSession: AsyncSession,
    client: AsyncClient,
) -> None:
    """admin PATCH /api/v1/users/{id} 设置 restrictedAccess=false，清除标记。"""
    # 建一个新用户并先赋予 restricted_access
    r = await client.post(
        "/api/v1/users",
        headers=AUTH_ADMIN,
        json={
            "username": "test_ra_clear",
            "display_name": "Test RA Clear",
            "email": None,
            "password": "TestPass123!",
        },
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    # 先赋予
    r2 = await client.patch(
        f"/api/v1/users/{user_id}",
        headers=AUTH_ADMIN,
        json={"restrictedAccess": True},
    )
    assert r2.status_code == 200, r2.text

    # 再清除
    r3 = await client.patch(
        f"/api/v1/users/{user_id}",
        headers=AUTH_ADMIN,
        json={"restrictedAccess": False},
    )
    assert r3.status_code == 200, r3.text

    user_row = await dbSession.get(User, user_id)
    assert user_row.restricted_access is False, (
        "restricted_access should be False after clearing"
    )
