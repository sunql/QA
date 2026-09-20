"""getCurrentUser 解析逻辑单测（feat-user-auth）。

只覆盖 stub 头解析 + AUTH_MODE 开关的纯逻辑分支（不需要 DB）。
DB 集成场景见 ``app/tests/integration/test_get_current_user_bearer.py``。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import getSettings
from app.dependencies import (
    DEFAULT_STUB_ROLES,
    DEFAULT_STUB_USER_ID,
    getCurrentUser,
)
from app.domain.exceptions import PermissionDeniedError


def _stub_session_returning_none() -> MagicMock:
    """构造返回 ``None`` 的 session mock（_resolveDbUser 走查无分支）。"""
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    session.get = AsyncMock(return_value=None)
    return session


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


class TestStubHeaderDefaults:
    async def test_anonymous_returns_default_user_and_admin_roles(self) -> None:
        """无任何 header → DEFAULT_STUB_USER_ID + DEFAULT_STUB_ROLES。"""
        session = _stub_session_returning_none()
        result = await getCurrentUser(
            authorization=None,
            xUserId=None,
            xTenantId=None,
            xUserRoles=None,
            xUserDepartments=None,
            session=session,
        )
        assert result.userId == DEFAULT_STUB_USER_ID
        assert result.roles == DEFAULT_STUB_ROLES
        assert result.dbUserId is None

    async def test_x_user_id_with_roles_header_uses_header(self) -> None:
        """stub 模式 + X-User-Id 查无 → header 角色生效（默认 dev 体验）。"""
        session = _stub_session_returning_none()
        result = await getCurrentUser(
            authorization=None,
            xUserId="ghost",
            xTenantId="t1",
            xUserRoles="analyst",
            xUserDepartments=None,
            session=session,
        )
        assert result.userId == "ghost"
        assert result.roles == ("analyst",)
        assert result.dbUserId is None


class TestAuthModeReal:
    """AUTH_MODE=real：无 Bearer → 403。"""

    async def test_real_mode_without_bearer_raises_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", False)
        monkeypatch.setattr(settings, "authStubEnabled", True)

        session = _stub_session_returning_none()
        with pytest.raises(PermissionDeniedError) as exc_info:
            await getCurrentUser(
                authorization=None,
                xUserId="anyone",
                xTenantId=None,
                xUserRoles="admin",
                xUserDepartments=None,
                session=session,
            )
        assert exc_info.value.message == "请先登录"

    async def test_real_mode_with_allow_stub_still_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ALLOW_STUB_WHEN_REAL=true 时 real 模式仍走 stub（过渡期）。"""
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", True)

        session = _stub_session_returning_none()
        result = await getCurrentUser(
            authorization=None,
            xUserId="u1",
            xTenantId=None,
            xUserRoles="analyst",
            xUserDepartments=None,
            session=session,
        )
        assert result.userId == "u1"
        assert result.roles == ("analyst",)


class TestAuthStubDisabled:
    """AUTH_STUB_ENABLED=0：即便 stub 模式也拒绝 stub 头（保持原行为）。"""

    async def test_stub_disabled_without_bearer_raises_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "stub")
        monkeypatch.setattr(settings, "authStubEnabled", False)

        session = _stub_session_returning_none()
        with pytest.raises(PermissionDeniedError):
            await getCurrentUser(
                authorization=None,
                xUserId="u1",
                xTenantId=None,
                xUserRoles="admin",
                xUserDepartments=None,
                session=session,
            )