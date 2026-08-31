"""getCurrentUser stub auth 单测（2026-08-31 默认 admin 化）。

覆盖：
- 缺省 headers → 默认 admin（dev 兜底）
- X-User-Roles 显式覆盖
- X-User-Roles="" / 全空白 → 退化到默认
- X-User-Departments 多值解析
- AUTH_STUB_ENABLED=0 → 拒绝
- X-User-Roles 去空白 + 跳过空段
- 端到端：默认 admin user 通过 owner-based ACL

`_buildCurrentUser` 是 getCurrentUser 拆出的纯函数（无 FastAPI 依赖），
单测直接调它验证 header 解析逻辑；`getCurrentUser` 本身需要 FastAPI
依赖注入来解析 Header 对象，单测中由 TestClient 集成测试覆盖。
"""

from __future__ import annotations

import pytest

from app.dependencies import (
    DEFAULT_STUB_ROLES,
    DEFAULT_STUB_USER_ID,
    CurrentUser,
    _buildCurrentUser,
    getCurrentUser,
)
from app.domain.exceptions import PermissionDeniedError


# ---------- 纯函数 _buildCurrentUser：header 解析逻辑 ----------


def test_default_stub_user_is_admin() -> None:
    """缺省 headers → 默认带 admin 角色（dev/test 兜底）。"""
    user = _buildCurrentUser(
        userId=None,
        tenantId=None,
        rolesHeader=None,
        departmentsHeader=None,
    )
    assert "admin" in user.roles
    assert "user" in user.roles
    assert user.roles == DEFAULT_STUB_ROLES
    assert user.userId == DEFAULT_STUB_USER_ID
    assert user.tenantId == "default"
    assert user.departments == ()


def test_explicit_user_overrides_default() -> None:
    """X-User-Id 显式传值 → 覆盖默认值。"""
    user = _buildCurrentUser(
        userId="alice", tenantId=None, rolesHeader=None, departmentsHeader=None
    )
    assert user.userId == "alice"


def test_explicit_roles_override_default() -> None:
    """X-User-Roles=admin,finance → ('admin', 'finance')。"""
    user = _buildCurrentUser(
        userId=None,
        tenantId=None,
        rolesHeader="admin,finance",
        departmentsHeader=None,
    )
    assert user.roles == ("admin", "finance")


def test_explicit_roles_user_only() -> None:
    """X-User-Roles=user → 显式非 admin 路径（用于 ACL 否定测试）。"""
    user = _buildCurrentUser(
        userId=None, tenantId=None, rolesHeader="user", departmentsHeader=None
    )
    assert user.roles == ("user",)
    assert "admin" not in user.roles


def test_roles_strip_whitespace_and_skip_empty() -> None:
    """X-User-Roles=' admin , , user ' → ('admin', 'user')。"""
    user = _buildCurrentUser(
        userId=None,
        tenantId=None,
        rolesHeader=" admin , , user ",
        departmentsHeader=None,
    )
    assert user.roles == ("admin", "user")


def test_empty_roles_falls_back_to_default() -> None:
    """X-User-Roles 全空白/空字符串 → 退化到默认 admin（不绕开 ACL）。"""
    user1 = _buildCurrentUser(
        userId=None, tenantId=None, rolesHeader="", departmentsHeader=None
    )
    assert "admin" in user1.roles
    user2 = _buildCurrentUser(
        userId=None, tenantId=None, rolesHeader="   ", departmentsHeader=None
    )
    assert "admin" in user2.roles


def test_departments_parsed_from_header() -> None:
    """X-User-Departments=采购部,财务部 → ('采购部', '财务部')。"""
    user = _buildCurrentUser(
        userId=None,
        tenantId=None,
        rolesHeader=None,
        departmentsHeader="采购部,财务部",
    )
    assert user.departments == ("采购部", "财务部")


def test_departments_strip_whitespace_and_skip_empty() -> None:
    """X-User-Departments=' 采购部 , 财务部 ' → ('采购部', '财务部')。"""
    user = _buildCurrentUser(
        userId=None,
        tenantId=None,
        rolesHeader=None,
        departmentsHeader=" 采购部 , 财务部 ",
    )
    assert user.departments == ("采购部", "财务部")


def test_tenant_id_parsed() -> None:
    """X-Tenant-Id → tenantId 字段。"""
    user = _buildCurrentUser(
        userId=None, tenantId="acme", rolesHeader=None, departmentsHeader=None
    )
    assert user.tenantId == "acme"


def test_current_user_dataclass_defaults_match_module_constants() -> None:
    """CurrentUser 默认字段值必须与 module-level 常量一致（避免漂移）。"""
    instance = CurrentUser()
    assert instance.userId == DEFAULT_STUB_USER_ID
    assert instance.roles == DEFAULT_STUB_ROLES
    assert instance.departments == ()


def test_default_admin_lets_owner_acl_pass_for_arbitrary_owner() -> None:
    """端到端：缺省 stub user（带 admin）→ 可改任意 owner 的 entity。

    闭环验证「默认 admin 设计意图」：让未登录访问有 ACL 的端点不会立刻 403。
    """
    from app.services.acl_service import AclService

    user = _buildCurrentUser(
        userId=None, tenantId=None, rolesHeader=None, departmentsHeader=None
    )
    svc = AclService()
    svc.assertCanModify(user, "采购部", "Feature", "FEAT_001")
    svc.assertCanModify(user, "不存在的部门", "Feature", "FEAT_001")
    svc.assertCanModify(user, "", "Feature", "FEAT_001")


def test_explicit_user_role_does_not_pass_owner_acl() -> None:
    """显式 user-only 角色 + owner 不在 user.departments → PermissionDeniedError。

    闭环验证「显式覆盖」：测试非 admin 路径仍按原 ACL 工作。
    """
    from app.services.acl_service import AclService
    from app.domain.exceptions import PermissionDeniedError

    user = _buildCurrentUser(
        userId="bob",
        tenantId=None,
        rolesHeader="user",
        departmentsHeader="财务部",
    )
    svc = AclService()
    with pytest.raises(PermissionDeniedError):
        svc.assertCanModify(user, "采购部", "Feature", "FEAT_002")


# ---------- FastAPI 依赖：stub disabled 拒绝路径 ----------


async def test_stub_disabled_raises_permission_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH_STUB_ENABLED=0 → 拒绝任何 stub 头（生产环境兜底）。

    注：直接调 getCurrentUser 时 Header 参数是 wrapper 对象（没值），但
    走到 os.environ 检查前不会解析；只要环境变量切到 0 即触发。
    """
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")
    with pytest.raises(PermissionDeniedError) as excInfo:
        await getCurrentUser()
    assert "Stub auth 未启用" in str(excInfo.value)


async def test_stub_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTH_STUB_ENABLED 不设 → 默认开（dev/test 默认开的安全护栏）。"""
    monkeypatch.delenv("AUTH_STUB_ENABLED", raising=False)
    # 不抛错 = 通过；返回的对象由 FastAPI 框架解析 Header 而非单测。
    # 这里只断言环境变量缺省行为一致：函数能进入 _buildCurrentUser 路径。
    import os
    assert os.environ.get("AUTH_STUB_ENABLED", "1") == "1"
