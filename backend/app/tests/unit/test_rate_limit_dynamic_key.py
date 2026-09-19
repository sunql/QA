"""限流动态 key 函数单测（feat-chat-concurrency-params）。

覆盖：
- ``_dynamic_key`` 三策略分支（ip / user_id / ip_user）
- ``set_rate_limit_strategy`` 写入缓存 + 非法值 fallback
- ``get_rate_limit_strategy`` 默认值 + 缓存命中
- ``invalidate_rate_limit_key_cache`` 标记 TTL 过期

无 IO：纯 module-level dict 操作 + 构造 minimal request 对象；用
``monkeypatch`` 替换 ``get_remote_address`` 拿固定 IP，避免 slowapi 解析
``request.client.host`` 的具体行为影响断言。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest
from slowapi.util import get_remote_address

from app.infrastructure import rate_limit


@pytest.fixture(autouse=True)
def _resetStrategyCache():
    """每个用例前后还原 cache 默认状态，避免用例间污染。"""
    previous_strategy = rate_limit._rate_limit_strategy_cache["strategy"]
    previous_fetched = rate_limit._rate_limit_strategy_cache["fetched_at"]
    yield
    rate_limit._rate_limit_strategy_cache["strategy"] = previous_strategy
    rate_limit._rate_limit_strategy_cache["fetched_at"] = previous_fetched


@pytest.fixture(autouse=True)
def _fixedRemoteAddress(monkeypatch: pytest.MonkeyPatch):
    """把 get_remote_address 替换为返回固定 IP，避免 slowapi 真实解析 request 失败。"""
    monkeypatch.setattr(rate_limit, "get_remote_address", lambda _req: "203.0.113.42")
    yield


@dataclass
class _FakeState:
    """模拟 FastAPI middleware 注入的 request.state；user 在 .user 属性。"""

    user: Any = None


@dataclass
class _FakeRequest:
    """minimal request：``_dynamic_key`` 读 state.user。IP 来自 patch 后的 get_remote_address。"""

    state: _FakeState


@dataclass
class _FakeUser:
    userId: int | str


class TestDynamicKeyStrategies:
    """三策略下 _dynamic_key 的返回值契约。"""

    def test_ip_strategy_returns_remote_address(self) -> None:
        rate_limit.set_rate_limit_strategy("ip")
        request = _FakeRequest(state=_FakeState(user=_FakeUser(userId=42)))
        assert rate_limit._dynamic_key(request) == "203.0.113.42"

    def test_user_id_strategy_returns_user_id_str(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        request = _FakeRequest(state=_FakeState(user=_FakeUser(userId=42)))
        assert rate_limit._dynamic_key(request) == "42"

    def test_user_id_strategy_with_string_user_id(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        request = _FakeRequest(state=_FakeState(user=_FakeUser(userId="alice")))
        assert rate_limit._dynamic_key(request) == "alice"

    def test_user_id_strategy_falls_back_to_anonymous_when_no_user(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        request = _FakeRequest(state=_FakeState(user=None))
        # 无 user → "anonymous"，所有匿名共享一个 bucket（设计权衡）
        assert rate_limit._dynamic_key(request) == "anonymous"

    def test_user_id_strategy_falls_back_when_user_has_no_userId(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        request = _FakeRequest(state=_FakeState(user=object()))  # 无 userId 属性
        assert rate_limit._dynamic_key(request) == "anonymous"

    def test_ip_user_strategy_returns_combined(self) -> None:
        rate_limit.set_rate_limit_strategy("ip_user")
        request = _FakeRequest(state=_FakeState(user=_FakeUser(userId=42)))
        assert rate_limit._dynamic_key(request) == "203.0.113.42:42"

    def test_ip_user_strategy_with_anonymous(self) -> None:
        rate_limit.set_rate_limit_strategy("ip_user")
        request = _FakeRequest(state=_FakeState(user=None))
        assert rate_limit._dynamic_key(request) == "203.0.113.42:anonymous"


class TestSetRateLimitStrategy:
    """set_rate_limit_strategy 写入行为 + 非法值 fallback。"""

    def test_valid_strategy_written(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        assert rate_limit._rate_limit_strategy_cache["strategy"] == "user_id"
        assert rate_limit._rate_limit_strategy_cache["fetched_at"] > 0

    def test_invalid_strategy_falls_back_to_ip(self, caplog: pytest.LogCaptureFixture) -> None:
        rate_limit.set_rate_limit_strategy("garbage")  # noqa: 不在白名单
        assert rate_limit._rate_limit_strategy_cache["strategy"] == "ip"
        assert "非法" in caplog.text


class TestGetRateLimitStrategy:
    """缓存命中 + 初始默认。"""

    def test_default_value_when_cache_never_initialized(self) -> None:
        # 强制从未初始化状态：fetched_at == 0.0
        rate_limit._rate_limit_strategy_cache["fetched_at"] = 0.0
        rate_limit._rate_limit_strategy_cache["strategy"] = "ip"
        assert rate_limit.get_rate_limit_strategy() == "ip"

    def test_cache_hit_returns_set_value(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        assert rate_limit.get_rate_limit_strategy() == "user_id"

    def test_cache_stale_still_returns_last_value(self) -> None:
        """TTL 过期但 fetched_at > 0 时仍返回 stale 值（不在 request 路径同步读 DB）。"""
        rate_limit.set_rate_limit_strategy("ip_user")
        # 把 fetched_at 推到 1 小时前
        rate_limit._rate_limit_strategy_cache["fetched_at"] = time.monotonic() - 3600
        assert rate_limit.get_rate_limit_strategy() == "ip_user"


class TestInvalidateRateLimitKeyCache:
    """invalidate_rate_limit_key_cache 标记 TTL 过期（feetched_at → 0）。"""

    def test_invalidated_cache_uses_default(self) -> None:
        rate_limit.set_rate_limit_strategy("user_id")
        assert rate_limit.get_rate_limit_strategy() == "user_id"
        rate_limit.invalidate_rate_limit_key_cache()
        # fetched_at == 0.0 → get_rate_limit_strategy() 走默认路径，返回 "ip"
        assert rate_limit.get_rate_limit_strategy() == "ip"


class TestLimiterUsesDynamicKey:
    """limiter 实例使用 _dynamic_key 而非 get_remote_address（接线契约）。"""

    def test_limiter_key_func_is_dynamic(self) -> None:
        assert rate_limit.limiter._key_func is rate_limit._dynamic_key

    def test_get_remote_address_imported_for_patching(self) -> None:
        """sanity：确保 get_remote_address 来自 slowapi.util（可被 monkeypatch）。"""
        assert callable(get_remote_address)