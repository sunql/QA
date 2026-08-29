"""Token 计数器工厂。

按 LLM provider 类型选择计数器并缓存单例。
"""

from __future__ import annotations

from app.domain.enums import ProviderType
from app.infrastructure.token_counter.base_counter import TokenCounter
from app.infrastructure.token_counter.heuristic_counter import HeuristicCounter
from app.infrastructure.token_counter.tiktoken_counter import TiktokenCounter

# provider -> 计数器单例
_registry: dict[ProviderType, TokenCounter] = {}

_TIKTOKEN_PROVIDERS = frozenset(
    {ProviderType.OPENAI, ProviderType.AZURE_OPENAI, ProviderType.OPENAI_COMPATIBLE_PROXY}
)


def getCounter(provider: ProviderType) -> TokenCounter:
    """返回指定 provider 的计数器单例。"""
    if provider not in _registry:
        if provider in _TIKTOKEN_PROVIDERS:
            _registry[provider] = TiktokenCounter()
        else:
            # OLLAMA 及未知 provider
            _registry[provider] = HeuristicCounter()
    return _registry[provider]


def resetRegistry() -> None:
    """清空缓存（测试用）。"""
    _registry.clear()
