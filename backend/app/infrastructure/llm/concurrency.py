"""LLM 并发控制闸（feat-chat-concurrency）。

独立的 module 避开 ``factory.py`` ↔ ``openai_client.py`` ↔ ``ollama_client.py``
的循环依赖。三方都从这里 import ``acquire_llm_concurrency``。

设计要点：
- 全局 ``asyncio.Semaphore`` 限制同时 in-flight 的 LLM HTTP 调用数（含
  OpenAI/Azure/DeepSeek/Qwen/Moonshot 兼容代理 + Ollama）。
- ``reload()`` 原子替换：创建新 Semaphore，旧请求持有的引用继续工作，
  不中断正在跑的 HTTP 调用。
- 期望调用方用 ``async with acquire_llm_concurrency():`` 在 HTTP 调用处包，
  保证异常路径也能 release。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app.config import getSettings

logger = logging.getLogger(__name__)

_llm_concurrency_manager: "LLMConcurrencyManager | None" = None


class LLMConcurrencyManager:
    """全局 LLM 并发控制闸。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"limit must be positive, got {limit}")
        self._sem: asyncio.Semaphore = asyncio.Semaphore(limit)

    @property
    def limit(self) -> int:
        """返回当前 Semaphore 的 limit（已 acquire 的不计入）。"""
        return self._sem._value  # type: ignore[attr-defined]  # noqa: SLF001

    @property
    def _semaphore(self) -> asyncio.Semaphore:
        return self._sem

    def reload(self, new_limit: int) -> None:
        """原子替换 Semaphore；旧请求持有的旧 Semaphore 引用继续工作。"""
        if new_limit < 1:
            raise ValueError(f"limit must be positive, got {new_limit}")
        self._sem = asyncio.Semaphore(new_limit)
        logger.info("LLMConcurrencyManager: limit reloaded to %d", new_limit)


def get_llm_concurrency_manager() -> LLMConcurrencyManager:
    """返回全局 LLMConcurrencyManager 单例（懒加载，默认 limit 来自 Settings）。"""
    global _llm_concurrency_manager
    if _llm_concurrency_manager is None:
        _llm_concurrency_manager = LLMConcurrencyManager(limit=getSettings().llmConcurrencyLimit)
    return _llm_concurrency_manager


def reload_llm_concurrency_limit(new_limit: int) -> None:
    """admin PUT 钩子调用：原子替换 limit。"""
    get_llm_concurrency_manager().reload(new_limit)


def reset_llm_concurrency_manager() -> None:
    """测试用：清空单例。下次 ``get_llm_concurrency_manager()`` 重新创建。"""
    global _llm_concurrency_manager
    _llm_concurrency_manager = None


@asynccontextmanager
async def acquire_llm_concurrency():
    """全局 LLM 并发闸 async with 包装。

    使用方式（OpenAiClient / OllamaClient 的 HTTP 调用处）::

        async with acquire_llm_concurrency():
            response = await self._client.chat.completions.create(**payload)

    异常路径由 async with 语义保证 release，不会泄漏计数。
    """
    manager = get_llm_concurrency_manager()
    sem = manager._semaphore  # noqa: SLF001
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()