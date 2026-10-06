"""真实 chat 路由限流测试（5.5，完整 API 链路）。

覆盖：压低限额后第 3 次请求返回 429（含完整流水线 fakes）。
其余限流契约测试（探针应用 / 中间件路径 / 测试环境关闭）留在 unit/test_rate_limit.py。

【迁移：真实 PG】由 unit/ 迁至 integration/（第四批）：client 走 integration/conftest.py
的 pgApiClient()（真实 PG + 每测试 TRUNCATE + ASGI 完整链路），dbSession 为真实 PG 会话
（Harness/rules/测试规范.md）；LLM 流水线仍 mock（自 test_chat_api.py 借 _seed/_installFakes）。
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.config import getSettings
from app.infrastructure.rate_limit import limiter


@pytest.fixture(autouse=True)
def _restoreLimiterState():
    """每个用例后恢复 limiter 开关并清空存储，避免共享单例在用例间泄漏。"""
    previous = limiter.enabled
    yield
    limiter.enabled = previous
    limiter.reset()


class TestRateLimitChatRoute:
    async def test_chat_route_exceeding_limit_returns_429(
        self, client, dbSession, monkeypatch
    ) -> None:
        """真实 chat 路由：压低限额后第 3 次请求返回 429（含完整流水线 fakes）。"""
        from app.tests.integration.test_chat_api import _installFakes, _seed

        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        previous = limiter.enabled
        limiter.enabled = True
        limiter.reset()
        getSettings.cache_clear()
        os.environ["RATE_LIMIT_REQUESTS"] = "2"
        os.environ["RATE_LIMIT_WINDOW"] = "minute"
        try:
            payload = {"sessionId": "s1", "question": "各供应商的收货数量汇总", "datasourceId": ds.id}
            await client.post("/api/v1/chat", json=payload)
            await client.post("/api/v1/chat", json=payload)
            exceeded = await client.post("/api/v1/chat", json=payload)
            assert exceeded.status_code == 429
            body = exceeded.json()
            assert body["success"] is False
            assert "频繁" in body["error"]
        finally:
            limiter.enabled = previous
            getSettings.cache_clear()
            os.environ.pop("RATE_LIMIT_REQUESTS", None)
            os.environ.pop("RATE_LIMIT_WINDOW", None)


class TestRateLimitMultiStepResumeRoute:
    async def test_resume_route_exceeding_limit_returns_429(self, client) -> None:
        """真实 `/chat/multi-step/{runId}/resume`：压低限额后第 3 次请求返回 429。

        续跑是「跑全链路 LLM（真金白银）+ 长连接」的重端点，必须有路由级
        `@limiter.limit`（与 `/chat`、`/stream` 同款）。断言要求 429 出现在**第三次
        且前两次不是 429**：只断言「出现过 429」的话，限流落在别的分支上也会绿。

        run 故意用随机 uuid（不存在）：限流必须在**处理函数之前**生效，
        与 run 是否存在无关。
        """
        previous = limiter.enabled
        limiter.enabled = True
        limiter.reset()
        getSettings.cache_clear()
        os.environ["RATE_LIMIT_REQUESTS"] = "2"
        os.environ["RATE_LIMIT_WINDOW"] = "minute"
        try:
            url = f"/api/v1/chat/multi-step/{uuid.uuid4()}/resume"
            first = await client.post(url, json={})
            second = await client.post(url, json={})
            exceeded = await client.post(url, json={})

            assert first.status_code != 429, f"第一次不该触发限流：{first.status_code}"
            assert second.status_code != 429, f"第二次不该触发限流：{second.status_code}"
            assert exceeded.status_code == 429, (
                f"/resume 未被限流覆盖（第三次 {exceeded.status_code}）"
            )
            body = exceeded.json()
            assert body["success"] is False
            assert "频繁" in body["error"], body
        finally:
            limiter.enabled = previous
            getSettings.cache_clear()
            os.environ.pop("RATE_LIMIT_REQUESTS", None)
            os.environ.pop("RATE_LIMIT_WINDOW", None)
