"""``_optionalCurrentUser`` 单测（fix/public-route-rate-limit-allow-anonymous）。

修 prod bug：AUTH_MODE=real 下 /api/v1/auth/login 等公开端点被
``rateLimitDep`` 隐式 ``Depends(getCurrentUser)`` 误挡 403。

本测试覆盖：_optionalCurrentUser 是 rateLimitDep 专用的「可选身份」包装
——MSG_AUTH_REQUIRED 时返回 None，其他错误继续 raise。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import getSettings
from app.dependencies import getCurrentUser
from app.domain.error_messages import MSG_AUTH_REQUIRED
from app.domain.exceptions import PermissionDeniedError
from app.infrastructure import rate_limit


def _stub_session_returning_none() -> MagicMock:
    """session mock：_resolveDbUser 走「查无」分支。"""
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=lambda: None)
    )
    session.get = AsyncMock(return_value=None)
    return session


def _make_request(headers: dict[str, str] | None = None) -> MagicMock:
    """构造带指定 headers 的 mock Request。"""
    req = MagicMock()
    req.headers = headers or {}
    return req


@pytest.fixture(autouse=True)
def _ensure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    if not getSettings().jwtSecret:
        monkeypatch.setenv("JWT_SECRET", "x" * 32)


class TestOptionalCurrentUserSwallowsAuthRequired:
    """AUTH_MODE=real + 无 Bearer：getCurrentUser raise MSG_AUTH_REQUIRED，
    _optionalCurrentUser 必须降级为 None（rateLimitDep 才能继续走 IP bucket）。"""

    async def test_real_mode_no_headers_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", False)
        monkeypatch.setattr(settings, "authStubEnabled", True)

        result = await rate_limit._optionalCurrentUser(
            request=_make_request(),
            session=_stub_session_returning_none(),
        )
        assert result is None, (
            "AUTH_MODE=real 无 Bearer 必须降级为 None，"
            "否则 rateLimitDep 会再次抛 MSG_AUTH_REQUIRED 误挡公开端点"
        )

    async def test_real_mode_stub_headers_but_disabled_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """real + allowStubWhenReal=False：即便带 X-User-* 头也被拒 ⇒ None。"""
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", False)

        result = await rate_limit._optionalCurrentUser(
            request=_make_request({"x-user-id": "ghost", "x-user-roles": "admin"}),
            session=_stub_session_returning_none(),
        )
        assert result is None


class TestOptionalCurrentUserPreservesSemanticAuthErrors:
    """TOKEN_EXPIRED / TOKEN_INVALID / ACCOUNT_DISABLED 等带语义的 auth 错误
    不应被吞 —— 这些是「用户曾有 session」失败，路由层应 401 而非降级匿名。"""

    async def test_get_current_user_raises_message_unchanged_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """patch getCurrentUser 抛一个非 MSG_AUTH_REQUIRED 的 PermissionDeniedError
        ⇒ _optionalCurrentUser 必须再 raise 出去（不吞）。"""

        async def fake_raise(*_args, **_kwargs):
            raise PermissionDeniedError("token 失效了")

        monkeypatch.setattr(rate_limit, "getCurrentUser", fake_raise)

        with pytest.raises(PermissionDeniedError) as exc_info:
            await rate_limit._optionalCurrentUser(
                request=_make_request(),
                session=_stub_session_returning_none(),
            )
        assert exc_info.value.message == "token 失效了"
        # 关键断言：不是 MSG_AUTH_REQUIRED，所以不会降级
        assert exc_info.value.message != MSG_AUTH_REQUIRED


class TestOptionalCurrentUserDelegatesToGetCurrentUser:
    """正常路径：有 Bearer / stub headers → 必须透传调用 getCurrentUser。"""

    async def test_stub_mode_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "stub")
        monkeypatch.setattr(settings, "authStubEnabled", True)

        # stub 模式 + X-User-Id 查无 → 走默认 stub 用户
        # 注意：HTTP header 名走小写（Starlette Headers 大小写不敏感并归一化小写）
        result = await rate_limit._optionalCurrentUser(
            request=_make_request({"x-user-id": "alice", "x-user-roles": "analyst"}),
            session=_stub_session_returning_none(),
        )
        assert result is not None
        assert result.userId == "alice"
        assert result.roles == ("analyst",)

    async def test_real_mode_with_allow_stub_when_real_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """real + allowStubWhenReal=True：仍走 stub 解析（过渡期）。"""
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", True)

        result = await rate_limit._optionalCurrentUser(
            request=_make_request({"x-user-id": "u1"}),
            session=_stub_session_returning_none(),
        )
        assert result is not None
        assert result.userId == "u1"
