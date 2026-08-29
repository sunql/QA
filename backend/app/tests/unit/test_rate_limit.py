"""限流测试（5.5，纯逻辑 / 探针应用，无 IO）。

验证 slowapi 集成契约：
- 限额内请求正常通过（200）
- 超限返回 429 + 错误包络
- 测试环境默认关闭限流（conftest 强制 RATE_LIMIT_ENABLED=false）
真实 chat 路由的 429 测试已迁至 integration/test_rate_limit_chat_route.py
（【迁移：真实 PG】第四批，走 pgApiClient 完整链路）。
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import getSettings
from app.infrastructure.rate_limit import limiter, rateLimitExceededHandler


@pytest.fixture(autouse=True)
def _restoreLimiterState():
    """每个用例后恢复 limiter 开关并清空存储，避免共享单例在用例间泄漏。"""
    previous = limiter.enabled
    yield
    limiter.enabled = previous
    limiter.reset()


def _rateLimitedApp(limit: str) -> FastAPI:
    """构造一个套用共享 limiter / 处理器 / 中间件的探针应用。

    /probe 为装饰路由（端点内触发 429）；/plain 无装饰，
    走 default_limits（SlowAPIMiddleware 中间件路径触发 429）。
    """
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rateLimitExceededHandler)
    app.add_middleware(SlowAPIMiddleware)

    @app.get("/probe")
    @limiter.limit(limit)
    async def probe(request: Request) -> dict:
        return {"ok": True}

    @app.get("/plain")
    async def plain() -> dict:
        return {"ok": True}

    return app


class TestRateLimit:
    async def test_within_limit_returns_200(self) -> None:
        previous = limiter.enabled
        limiter.enabled = True
        try:
            limiter.reset()
            app = _rateLimitedApp("2/minute")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                first = await ac.get("/probe")
                second = await ac.get("/probe")
            assert first.status_code == 200
            assert second.status_code == 200
        finally:
            limiter.enabled = previous

    async def test_exceeding_limit_returns_429(self) -> None:
        previous = limiter.enabled
        limiter.enabled = True
        try:
            limiter.reset()
            app = _rateLimitedApp("2/minute")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.get("/probe")
                await ac.get("/probe")
                exceeded = await ac.get("/probe")
            assert exceeded.status_code == 429
            body = exceeded.json()
            assert body["success"] is False
            assert "频繁" in body["error"]
            # headers_enabled=False 时由统一处理器手动注入 Retry-After
            assert exceeded.headers.get("Retry-After") is not None
        finally:
            limiter.enabled = previous

    async def test_default_limits_path_returns_standard_envelope(self) -> None:
        """非装饰路由走中间件路径触发 429，也须返回标准错误包络（sync handler）。"""
        previous = limiter.enabled
        limiter.enabled = True
        getSettings.cache_clear()
        os.environ["RATE_LIMIT_REQUESTS"] = "2"
        try:
            limiter.reset()
            app = _rateLimitedApp("5/minute")  # /probe 宽松，/plain 命中 default 2/minute
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.get("/plain")
                await ac.get("/plain")
                exceeded = await ac.get("/plain")
            assert exceeded.status_code == 429
            body = exceeded.json()
            assert body["success"] is False
            assert "频繁" in body["error"]
        finally:
            limiter.enabled = previous
            getSettings.cache_clear()
            os.environ.pop("RATE_LIMIT_REQUESTS", None)

    def test_limiter_disabled_in_test_env(self) -> None:
        getSettings.cache_clear()
        assert getSettings().rateLimitEnabled is False
        assert limiter.enabled is False
