"""Token 计数器服务层封装。

为服务层提供按 provider 获取计数器的统一入口，保持分层（service 不直接依赖 infrastructure 细节时可用此封装）。
"""

from __future__ import annotations

from app.domain.enums import ProviderType
from app.infrastructure.token_counter.base_counter import TokenCounter
from app.infrastructure.token_counter.factory import getCounter


def providerTokenCounter(provider: ProviderType) -> TokenCounter:
    """返回指定 provider 的 Token 计数器。"""
    return getCounter(provider)
