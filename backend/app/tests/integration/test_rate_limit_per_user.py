"""Task 3.1: 限流按真实用户身份分桶，不混用 IP（Finding 7）。

真实 PG + 完整 API 链路。
验证：
1. 用户 A 耗尽自身限额，不影响用户 B
2. 限额用 RATE_LIMIT_REQUESTS=3 控制（快速触发）
"""

from __future__ import annotations

import os

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.infrastructure.rate_limit import limiter, set_rate_limit_strategy
from app.models.rbac import User


def _hash(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(rounds=4)).decode()


async def _seed_two_users(dbSession: AsyncSession) -> tuple[str, str]:
    """创建两个 enabled 用户并返回 (userA_id, userB_id)。"""
    user_a = User(
        username="rate_user_a",
        display_name="Rate User A",
        email=None,
        enabled=True,
        password_hash=_hash("Abcd1234"),
        must_change_password=False,
    )
    user_b = User(
        username="rate_user_b",
        display_name="Rate User B",
        email=None,
        enabled=True,
        password_hash=_hash("Abcd1234"),
        must_change_password=False,
    )
    dbSession.add(user_a)
    dbSession.add(user_b)
    await dbSession.commit()
    await dbSession.refresh(user_a)
    await dbSession.refresh(user_b)
    return str(user_a.id), str(user_b.id)


async def _login_get_token(
    client: AsyncClient, username: str, password: str = "Abcd1234"
) -> str:
    """调用 /auth/login 返回 accessToken。"""
    r = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, f"login failed: {r.json()}"
    return r.json()["accessToken"]


@pytest.fixture(autouse=True)
def _restore_limiter_state():
    """每个测试后恢复 limiter 状态，避免跨测试泄漏。"""
    previous_enabled = limiter.enabled
    yield
    limiter.enabled = previous_enabled
    limiter.reset()


class TestRateLimitPerUser:
    """Finding 7: 限流必须按真实用户身份分桶，不混用 IP。

    场景：用户 A 耗尽限额 → 用户 B 仍可正常访问。
    """

    async def test_user_id_strategy_isolates_users(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户 A 超限后，用户 B 仍可访问（限流按 user_id 分桶）。"""
        # 确保 JWT secret 存在
        if not getSettings().jwtSecret:
            monkeypatch.setenv("JWT_SECRET", "x" * 32)

        # 设置小限额（3/minute）让测试快速触发
        previous = limiter.enabled
        limiter.enabled = True
        limiter.reset()
        os.environ["RATE_LIMIT_REQUESTS"] = "3"
        os.environ["RATE_LIMIT_WINDOW"] = "minute"
        # Finding 7: 使用 user_id 策略（按用户分桶，不混用 IP）
        set_rate_limit_strategy("user_id")
        getSettings.cache_clear()

        try:
            # 创建两个用户
            user_a_id, user_b_id = await _seed_two_users(dbSession)
            await dbSession.commit()

            # 各自登录获取 token
            token_a = await _login_get_token(client, "rate_user_a")
            token_b = await _login_get_token(client, "rate_user_b")

            # 用户 A 连续 3 次成功调用（耗尽限额）
            for i in range(3):
                r = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {token_a}"},
                )
                assert r.status_code == 200, (
                    f"user A request {i+1} 失败: {r.status_code} {r.json() if r.status_code != 200 else ''}"
                )

            # 用户 A 再次调用 → 应触发限流 429
            r_a_4th = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert r_a_4th.status_code == 429, (
                f"user A 第 4 次请求应被限流，实际: {r_a_4th.status_code}"
            )

            # 用户 B 首次调用 → 应成功（独立 bucket）
            r_b = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert r_b.status_code == 200, (
                f"user B 请求应成功，实际: {r_b.status_code} {r_b.json() if r_b.status_code != 200 else ''}"
            )

        finally:
            limiter.enabled = previous
            limiter.reset()
            set_rate_limit_strategy("ip")  # 恢复默认
            getSettings.cache_clear()
            os.environ.pop("RATE_LIMIT_REQUESTS", None)
            os.environ.pop("RATE_LIMIT_WINDOW", None)
            getSettings.cache_clear()
            os.environ.pop("RATE_LIMIT_REQUESTS", None)
            os.environ.pop("RATE_LIMIT_WINDOW", None)
