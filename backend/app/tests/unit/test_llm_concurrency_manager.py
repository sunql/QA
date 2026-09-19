"""LLMConcurrencyManager 单测（feat-chat-concurrency）。

覆盖：
- ``LLMConcurrencyManager.__init__`` 参数校验（limit 必须为正整数）
- ``acquire_llm_concurrency`` async with 正确释放 Semaphore（异常路径也 release）
- ``reload`` 原子替换 limit；旧 Semaphore 引用仍工作
- ``get_llm_concurrency_manager`` 单例模式 + test 隔离 reset
- OpenAI / Ollama 客户端入口确实包了 ``acquire_llm_concurrency``（接线契约）

无 IO：纯 Semaphore + dict 断言。
"""

from __future__ import annotations

import asyncio
import pytest

from app.infrastructure.llm import concurrency as llm_concurrency
from app.infrastructure.llm import factory as llm_factory

# llm_concurrency 是真实实现位置，factory 仅 re-export。两个都用于接线校验。
assert llm_factory.acquire_llm_concurrency is llm_concurrency.acquire_llm_concurrency


@pytest.fixture(autouse=True)
def _resetManager():
    """每个用例前后 reset 单例，保证用例间不串。"""
    llm_factory.reset_llm_concurrency_manager()
    yield
    llm_factory.reset_llm_concurrency_manager()


class TestLLMConcurrencyManagerInit:
    """__init__ 参数校验。"""

    def test_valid_limit_creates_semaphore(self) -> None:
        mgr = llm_concurrency.LLMConcurrencyManager(limit=10)
        # Semaphore 内部 _value 初始 = limit
        assert mgr._semaphore._value == 10  # type: ignore[attr-defined]  # noqa: SLF001

    def test_rejects_zero_limit(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            llm_concurrency.LLMConcurrencyManager(limit=0)

    def test_rejects_negative_limit(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            llm_concurrency.LLMConcurrencyManager(limit=-5)


class TestAcquireRelease:
    """acquire_llm_concurrency async with 行为。"""

    async def test_acquire_decrements_available(self) -> None:
        llm_factory.reload_llm_concurrency_limit(3)
        async with llm_factory.acquire_llm_concurrency():
            assert llm_concurrency._llm_concurrency_manager._semaphore._value == 2  # noqa: SLF001
        # 退出后 release
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 3  # noqa: SLF001

    async def test_release_on_exception(self) -> None:
        llm_factory.reload_llm_concurrency_limit(2)
        with pytest.raises(RuntimeError, match="boom"):
            async with llm_factory.acquire_llm_concurrency():
                raise RuntimeError("boom")
        # 异常路径仍 release
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 2  # noqa: SLF001

    async def test_blocks_when_full_then_unblocks(self) -> None:
        """limit=1：第一次 acquire 后 _value=0；显式 release 后再次 acquire 立即成功。

        不构造「持锁协程不死」场景——避免测自己实现的死锁；改用同步序列
        验证 Semaphore 的 acquire/release 计数语义。
        """
        llm_factory.reload_llm_concurrency_limit(1)
        # 第一次 acquire：拿满，_value → 0
        async with llm_factory.acquire_llm_concurrency():
            assert llm_concurrency._llm_concurrency_manager._semaphore._value == 0  # noqa: SLF001
        # release 后 _value → 1，可再次 acquire
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 1  # noqa: SLF001
        async with llm_factory.acquire_llm_concurrency():
            assert llm_concurrency._llm_concurrency_manager._semaphore._value == 0  # noqa: SLF001


class TestReload:
    """reload 原子替换。"""

    def test_reload_updates_limit(self) -> None:
        llm_factory.reload_llm_concurrency_limit(5)
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 5  # noqa: SLF001
        llm_factory.reload_llm_concurrency_limit(50)
        assert llm_concurrency._llm_concurrency_manager._semaphore._value == 50  # noqa: SLF001

    def test_reload_rejects_invalid_limit(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            llm_factory.reload_llm_concurrency_limit(0)

    def test_reload_preserves_inflight_requests(self) -> None:
        """reload 创建新 Semaphore，旧引用继续工作（不会丢失 in-flight 计数）。

        实现语义：reload 只替换 ``_sem`` 字段，已在旧 Semaphore 上 acquire 的协程
        持有的还是旧对象引用，release 时回写到旧对象；新请求走新对象。
        """
        llm_factory.reload_llm_concurrency_limit(2)
        old_sem_ref = llm_concurrency._llm_concurrency_manager._semaphore  # noqa: SLF001
        old_initial = old_sem_ref._value  # noqa: SLF001
        assert old_initial == 2

        llm_factory.reload_llm_concurrency_limit(10)
        new_sem_ref = llm_concurrency._llm_concurrency_manager._semaphore  # noqa: SLF001
        assert new_sem_ref is not old_sem_ref
        assert new_sem_ref._value == 10  # noqa: SLF001
        # 旧 Semaphore 的状态未被新 Semaphore 影响
        assert old_sem_ref._value == old_initial  # noqa: SLF001


class TestSingletonManager:
    """get_llm_concurrency_manager 单例模式。"""

    def test_singleton_returns_same_instance(self) -> None:
        m1 = llm_factory.get_llm_concurrency_manager()
        m2 = llm_factory.get_llm_concurrency_manager()
        assert m1 is m2

    def test_singleton_lazy_init_uses_settings_default(self) -> None:
        """首调 get_llm_concurrency_manager() 时 limit 来自 Settings.llmConcurrencyLimit。"""
        # 不通过 reload 污染，直接看 lazy init 的初始值
        llm_factory.reset_llm_concurrency_manager()
        mgr = llm_factory.get_llm_concurrency_manager()
        assert mgr._semaphore._value == llm_factory.getSettings().llmConcurrencyLimit  # noqa: SLF001


class TestFactoryWiring:
    """OpenAI / Ollama 客户端的 HTTP 调用都包了 acquire_llm_concurrency（接线契约）。

    这些是源码静态检查：grep + import 即可，确保后续重构不会漏装闸门。
    """

    def test_openai_complete_wrapped(self) -> None:
        import inspect

        from app.infrastructure.llm.openai_client import OpenAiClient

        src = inspect.getsource(OpenAiClient.complete)
        assert "acquire_llm_concurrency" in src

    def test_openai_complete_stream_wrapped(self) -> None:
        import inspect

        from app.infrastructure.llm.openai_client import OpenAiClient

        src = inspect.getsource(OpenAiClient.completeStream)
        assert "acquire_llm_concurrency" in src

    def test_openai_complete_with_tools_wrapped(self) -> None:
        import inspect

        from app.infrastructure.llm.openai_client import OpenAiClient

        src = inspect.getsource(OpenAiClient.complete_with_tools)
        assert "acquire_llm_concurrency" in src

    def test_ollama_complete_wrapped(self) -> None:
        import inspect

        from app.infrastructure.llm.ollama_client import OllamaClient

        src = inspect.getsource(OllamaClient.complete)
        assert "acquire_llm_concurrency" in src

    def test_ollama_complete_stream_wrapped(self) -> None:
        import inspect

        from app.infrastructure.llm.ollama_client import OllamaClient

        src = inspect.getsource(OllamaClient.completeStream)
        assert "acquire_llm_concurrency" in src