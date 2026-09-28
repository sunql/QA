"""system_config admin PUT 副作用单测（feat-chat-concurrency-params + feat-chat-concurrency）。

覆盖：
- ``_applyRuntimeSideEffect`` 对 RATE_LIMIT_KEY_STRATEGY 立即刷新 rate_limit 缓存
- 对 LLM_CONCURRENCY_LIMIT 立即刷新 LLMConcurrencyManager
- 对 DB_POOL_SIZE / DB_MAX_OVERFLOW 仅写 warning，不动 runtime
- 对未知 key 完全不触发副作用
- 副作用函数异常被吞（不应抛回 PUT 路由 → 单调失败不阻塞响应）

无 IO：纯函数 + module-level dict 断言。
"""

from __future__ import annotations

import pytest

from app.api.v1 import system_config as system_config_router
from app.infrastructure import rate_limit
from app.infrastructure.llm import concurrency as llm_concurrency
from app.infrastructure.llm import factory as llm_factory


@pytest.fixture(autouse=True)
def _resetRateLimitCache():
    """每个用例前后还原 rate_limit 缓存到默认状态。"""
    previous_strategy = rate_limit._rate_limit_strategy_cache["strategy"]
    previous_fetched = rate_limit._rate_limit_strategy_cache["fetched_at"]
    previous_limit = (
        llm_concurrency._llm_concurrency_manager._semaphore._value  # noqa: SLF001
        if llm_concurrency._llm_concurrency_manager is not None
        else None
    )
    yield
    rate_limit._rate_limit_strategy_cache["strategy"] = previous_strategy
    rate_limit._rate_limit_strategy_cache["fetched_at"] = previous_fetched
    if previous_limit is not None:
        llm_factory.reset_llm_concurrency_manager()


class TestRateLimitStrategySideEffect:
    """RATE_LIMIT_KEY_STRATEGY PUT 立即刷新 rate_limit 缓存。"""

    def test_valid_strategy_written_to_cache(self) -> None:
        system_config_router._applyRuntimeSideEffect("RATE_LIMIT_KEY_STRATEGY", "user_id")
        assert rate_limit.get_rate_limit_strategy() == "user_id"

    def test_none_value_treated_as_default(self) -> None:
        """payload.value 为 None 时按 "ip" 处理（与 set_rate_limit_strategy 一致）。"""
        system_config_router._applyRuntimeSideEffect("RATE_LIMIT_KEY_STRATEGY", None)
        assert rate_limit.get_rate_limit_strategy() == "ip"

    def test_invalid_strategy_falls_back_to_ip(self) -> None:
        system_config_router._applyRuntimeSideEffect("RATE_LIMIT_KEY_STRATEGY", "garbage")
        assert rate_limit.get_rate_limit_strategy() == "ip"

    def test_garbage_value_still_does_not_throw(self) -> None:
        """副作用必须单调成功：内部 set_rate_limit_strategy 已 sanitized。"""
        # 不应抛错
        system_config_router._applyRuntimeSideEffect("RATE_LIMIT_KEY_STRATEGY", "garbage")
        # 缓存已 fallback 到 "ip"，可继续服务
        assert rate_limit.get_rate_limit_strategy() == "ip"


class TestDbPoolSideEffect:
    """DB_POOL_SIZE / DB_MAX_OVERFLOW 仅写 warning 日志，不动 runtime。"""

    def test_db_pool_size_logs_warning_only(self, caplog: pytest.LogCaptureFixture) -> None:
        before = dict(rate_limit._rate_limit_strategy_cache)  # 顺带验证不影响其他缓存
        system_config_router._applyRuntimeSideEffect("DB_POOL_SIZE", "20")
        # 不应改任何缓存
        assert rate_limit._rate_limit_strategy_cache == before
        assert "DB_POOL_SIZE" in caplog.text
        assert "重启容器" in caplog.text

    def test_db_max_overflow_logs_warning_only(self, caplog: pytest.LogCaptureFixture) -> None:
        before = dict(rate_limit._rate_limit_strategy_cache)
        system_config_router._applyRuntimeSideEffect("DB_MAX_OVERFLOW", "30")
        assert rate_limit._rate_limit_strategy_cache == before
        assert "DB_MAX_OVERFLOW" in caplog.text
        assert "重启容器" in caplog.text


class TestUnknownKeySideEffect:
    """未列入副作用表的 key 完全不触发任何动作。"""

    def test_unknown_key_does_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        before = dict(rate_limit._rate_limit_strategy_cache)
        system_config_router._applyRuntimeSideEffect("ENABLE_L4_AGENT_LOOP", "true")
        # 缓存未变
        assert rate_limit._rate_limit_strategy_cache == before
        # 不应有 warning 日志
        assert "ENABLE_L4_AGENT_LOOP" not in caplog.text


class TestSideEffectDoesNotThrow:
    """副作用函数被设计成「单调失败吞掉」，不抛回 PUT 路由。"""

    def test_rate_limit_module_import_failure_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """即便 rate_limit 模块 import 失败也不应抛回路由（防御性编程）。"""
        # 模拟 set_rate_limit_strategy 抛异常
        def boom(_value: str) -> None:
            raise RuntimeError("rate_limit cache broken")

        monkeypatch.setattr(rate_limit, "set_rate_limit_strategy", boom)
        # 不应抛错
        system_config_router._applyRuntimeSideEffect("RATE_LIMIT_KEY_STRATEGY", "user_id")


class TestLLMConcurrencyLimitSideEffect:
    """LLM_CONCURRENCY_LIMIT PUT 立即刷新 LLMConcurrencyManager。"""

    def test_valid_value_written_to_manager(self) -> None:
        # 触发 lazy init
        llm_factory.get_llm_concurrency_manager()
        system_config_router._applyRuntimeSideEffect("LLM_CONCURRENCY_LIMIT", "10")
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 10  # noqa: SLF001

    def test_none_value_treated_as_default(self) -> None:
        llm_factory.get_llm_concurrency_manager()
        system_config_router._applyRuntimeSideEffect("LLM_CONCURRENCY_LIMIT", None)
        # 内部 int(None or "20")=20
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 20  # noqa: SLF001

    def test_invalid_value_warns_but_does_not_throw(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        llm_factory.get_llm_concurrency_manager()
        before = llm_concurrency._llm_concurrency_manager._semaphore._value  # noqa: SLF001
        # 不应抛错
        system_config_router._applyRuntimeSideEffect("LLM_CONCURRENCY_LIMIT", "garbage")
        # 值未变（reload 未触发）
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == before  # noqa: SLF001
        assert "非法" in caplog.text

    def test_zero_value_rejected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        llm_factory.get_llm_concurrency_manager()
        before = llm_concurrency._llm_concurrency_manager._semaphore._value  # noqa: SLF001
        system_config_router._applyRuntimeSideEffect("LLM_CONCURRENCY_LIMIT", "0")
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == before  # noqa: SLF001
        assert "非法" in caplog.text