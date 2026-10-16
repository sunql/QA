"""fix/public-route-rate-limit-allow-anonymous 集成测试。

Prod bug 复现：AUTH_MODE=real 下，``/api/v1/auth/login`` 与
``/api/v1/auth/password-policy`` 被 ``rateLimitDep`` 隐式
``Depends(getCurrentUser)`` 误挡 403 "请先登录"。

本测试覆盖：_optionalCurrentUser 接入后，这两个公开端点应正常返回
（login 401 invalid creds，password-policy 200 OK），**不被 rateLimitDep 挡**。
"""

from __future__ import annotations

import os

import pytest
from httpx import AsyncClient

from app.config import getSettings
from app.infrastructure.rate_limit import limiter


@pytest.fixture(autouse=True)
def _restore_limiter_state():
    """每个用例后恢复 limiter 开关（避免与限流 429 测试串味）。"""
    previous = limiter.enabled
    yield
    limiter.enabled = previous
    limiter.reset()


class TestAuthPublicRoutesNotBlockedByRateLimit:
    """AUTH_MODE=real 下，公开 auth 端点不能被 rateLimitDep 误挡 403。"""

    async def test_login_in_real_mode_not_403(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """修复前：login 返回 403 「请先登录」（rateLimitDep raise MSG_AUTH_REQUIRED）。
        修复后：login 进入业务逻辑，凭据错误返回 401，**不是 403**。
        """
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", False)
        monkeypatch.setattr(settings, "authStubEnabled", False)
        getSettings.cache_clear()
        try:
            response = await client.post(
                "/api/v1/auth/login",
                json={"username": "ghost", "password": "wrong"},
            )
            # 关键断言：不能是 403（修复前就是这个码）
            assert response.status_code != 403, (
                f"login 仍被 rateLimitDep 误挡 403：{response.text}"
            )
            # 凭据错误应返回 401；如果 DB 没有该用户，可能 401 或 404
            assert response.status_code in (401, 404, 422), (
                f"login 期望进入业务逻辑，got {response.status_code}: {response.text}"
            )
        finally:
            getSettings.cache_clear()

    async def test_password_policy_in_real_mode_returns_200(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """修复前：password-policy 返回 403（被 rateLimitDep 误挡）。
        修复后：200 OK + 密码策略 JSON。
        """
        settings = getSettings()
        monkeypatch.setattr(settings, "authMode", "real")
        monkeypatch.setattr(settings, "allowStubWhenReal", False)
        monkeypatch.setattr(settings, "authStubEnabled", False)
        getSettings.cache_clear()
        try:
            response = await client.get("/api/v1/auth/password-policy")
            assert response.status_code == 200, (
                f"password-policy 应 200（公开端点），got {response.status_code}: "
                f"{response.text}"
            )
            body = response.json()
            # PasswordPolicyRead 至少含 minLength
            assert "minLength" in body
        finally:
            getSettings.cache_clear()
