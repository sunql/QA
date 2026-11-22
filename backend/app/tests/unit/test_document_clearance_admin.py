"""文档密级过滤：admin 必须是全密级；普通用户仍是 L0/L1。

背景（2026-11-22 实测）：管理员上传 L2 文档后列表里看不到 —— 因为默认
clearance = ["L0","L1"]，而 admin 既无 compliance_officer 角色也不在 l3_acl。
管理员要维护文档目录 ⇒ admin 必须绕过密级过滤。
"""

from __future__ import annotations

from typing import Any

from app.dependencies import CurrentUser
from app.models.rbac import User
from app.services.document_clearance import (
    CLEARANCE_ALL,
    CLEARANCE_DEFAULT,
    buildClearanceFilter,
)


async def _seedUser(session: Any, *, username: str) -> int:
    user = User(
        username=username,
        display_name=username,
        password_hash="$2b$12$" + "x" * 53,
        must_change_password=False,
        restricted_access=False,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user.id


def _current(userId: int, roles: list[str]) -> CurrentUser:
    return CurrentUser(
        userId="tester",
        roles=tuple(roles),
        departments=(),
        dbUserId=userId,
    )


async def test_admin_sees_every_level(dbSession: Any) -> None:
    userId = await _seedUser(dbSession, username="clearance-admin")

    levels = await buildClearanceFilter(dbSession, _current(userId, ["admin"]))

    assert levels == CLEARANCE_ALL


async def test_plain_user_keeps_default_levels(dbSession: Any) -> None:
    userId = await _seedUser(dbSession, username="clearance-user")

    levels = await buildClearanceFilter(dbSession, _current(userId, ["analyst"]))

    assert levels == CLEARANCE_DEFAULT
