"""IdentityService.create_user 必须写 password_hash（bcrypt）+ must_change_password=True。

背景（feat-admin-user-password Task 2）：Task 1 给 UserCreate 加了 password 字段，
但 create_user 没有读取 dto.password → 落库 password_hash=NULL，admin 创建的用户无法登录。
本测试固定契约：create_user 必须服务端强校验 + bcrypt 散列 + 强制下次登录改密。

feat-user-onboarding（2026-09-20）：create_user 还必须默认授「个人中心」菜单权限，
否则新用户 /menu-config 返回空 → 前端 fallback。本测试固定：USER 主体 + item.profile 行必须存在。
"""

from __future__ import annotations

import pytest
import bcrypt
from sqlalchemy import text

from app.services.identity_service import IdentityService
from app.schemas.rbac import UserCreate
from app.dependencies import CurrentUser
from app.domain.exceptions import ValidationError


@pytest.mark.asyncio
async def test_create_user_hashes_password(dbSession):
    """create_user 必须写 password_hash（bcrypt 不可逆）+ must_change_password=True。"""
    actor = CurrentUser(
        userId="admin",
        tenantId="default",
        roles=("admin",),
        departments=(),
    )
    svc = IdentityService()
    plain = "ValidPass1"
    row = await svc.create_user(
        dbSession,
        UserCreate(username="alice", displayName="Alice", password=plain),
        actor,
    )
    await dbSession.commit()

    assert row.password_hash is not None
    assert row.password_hash != plain  # 不存明文
    # bcrypt 验证可解
    assert bcrypt.checkpw(plain.encode(), row.password_hash.encode())
    assert row.must_change_password is True  # 强制下次登录改密


@pytest.mark.asyncio
async def test_create_user_password_too_weak_raises(dbSession):
    """validate_password 拒绝纯数字（满足 min_length=8 但缺字母），应抛 ValidationError。"""
    actor = CurrentUser(
        userId="admin",
        tenantId="default",
        roles=("admin",),
        departments=(),
    )
    svc = IdentityService()
    # 8 字符无字母 "12345678" → 服务端 validate_password 应拒
    with pytest.raises(ValidationError):
        await svc.create_user(
            dbSession,
            UserCreate(username="bob", displayName="Bob", password="12345678"),
            actor,
        )


@pytest.mark.asyncio
async def test_create_user_grants_profile_menu(dbSession):
    """create_user 必须给新用户 USER 主体直接授 item.profile，避免 /menu-config 空集合。

    feat-user-onboarding 背景：admin 创建的用户通常没角色/没组织，
    PermissionService.computeEffective 的三维度合集（direct ∪ role ∪ org）全空，
    /menu-config 返回空 → 前端走 FALLBACK_NAV（feat-rbac-identity 设计之外的状态）。
    业务契约：新建即有 1 个「个人中心」入口，后续 admin 在 /admin/users 调组织/角色
    或在 /admin/menus 直接补 grant 扩展。
    """
    actor = CurrentUser(
        userId="admin",
        tenantId="default",
        roles=("admin",),
        departments=(),
    )
    svc = IdentityService()
    row = await svc.create_user(
        dbSession,
        UserCreate(
            username="charlie",
            displayName="Charlie",
            password="ValidPass1",
        ),
        actor,
    )
    await dbSession.commit()

    # 用 raw SQL 绕开 SQLAlchemy 2.x identity map 缓存（seed-upsert-pattern）
    grants = (
        await dbSession.execute(
            text(
                "SELECT subject_type, subject_id, menu_code "
                "FROM permission_grant "
                "WHERE subject_type='USER' AND subject_id=:uid"
            ),
            {"uid": row.id},
        )
    ).all()
    assert ("USER", row.id, "item.profile") in grants, (
        f"create_user must insert USER({row.id}, item.profile) grant; got {grants}"
    )
