"""LLM 客户端工厂。

根据 LlmConfig 的 provider 创建对应客户端，并按配置 id 缓存单例。
API Key 优先使用配置中的加密 key（解密），否则回退到环境变量。

feat-chat-concurrency: 并发闸 ``LLMConcurrencyManager`` 在独立的
``app.infrastructure.llm.concurrency`` 模块（避开工厂与客户端的循环依赖）。
本模块只 re-export 公共 API，保留 ``from app.infrastructure.llm.factory import
acquire_llm_concurrency`` 的旧 import 路径以减少扩散。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, getSettings
from app.domain.enums import ProviderType
from app.infrastructure.llm.base_client import BaseLlmClient
from app.infrastructure.llm.concurrency import (  # noqa: F401  re-export
    LLMConcurrencyManager,
    acquire_llm_concurrency,
    get_llm_concurrency_manager,
    reload_llm_concurrency_limit,
    reset_llm_concurrency_manager,
)
from app.infrastructure.llm.ollama_client import OllamaClient
from app.infrastructure.llm.openai_client import OpenAiClient
from app.infrastructure.url_validator import validateEndpointUrl
from app.infrastructure.security.crypto import decryptApiKey

logger = logging.getLogger(__name__)

# config_id -> 客户端单例（legacy 路径使用）
_clients: dict[int, BaseLlmClient] = {}


# ---------------------------------------------------------------------------
# 缓存路径：cache key = (config_id, updated_at_epoch, endpoint, key_cipher)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=128)
def _buildClient(
    config_id: int,
    updated_at_epoch: int,
    endpoint: str,
    key_cipher: str,
) -> BaseLlmClient:
    """构造 LLM 客户端（同步，LRU 缓存）。

    cache key 包含 updated_at_epoch，确保 endpoint/key 变更后 cache miss。
    """
    key = decryptApiKey(key_cipher)
    # 构造兼容 config 对象供 OpenAiClient 使用
    config = type("_CacheClientConfig", (), {
        "id": config_id,
        "model_name": "cached-model",
        "api_endpoint": endpoint,
        "api_key_encrypted": key_cipher,
    })()
    provider = ProviderType.OPENAI
    # audit-fx-1.1：生产环境阻断非 https / 私有 IP endpoint
    if endpoint:
        validateEndpointUrl(endpoint)
    return OpenAiClient(config, apiKey=key, provider=provider)


async def getOrCreateClient(
    configId: int,
    session: AsyncSession,
) -> BaseLlmClient:
    """加载 LlmConfig 并返回缓存的客户端（cache key 含 updated_at）。

    Args:
        configId: LlmConfig id
        session: AsyncSession（FastAPI 路由中由 Depends(getDb) 注入）。
    """
    from app.domain.models import LlmConfig

    cfg = await session.get(LlmConfig, configId)
    if cfg is None:
        raise ValueError(f"LlmConfig id={configId} not found")
    epoch = int(cfg.updated_time.timestamp())
    return _buildClient(
        cfg.id,
        epoch,
        cfg.api_endpoint or "",
        cfg.api_key_encrypted or "",
    )


def evictClient(configId: int) -> None:
    """清除整个 _buildClient LRU 缓存（belt-and-suspenders）。"""
    _buildClient.cache_clear()


def createClient(config: Any, *, settings: Settings | None = None, apiKey: str | None = None) -> BaseLlmClient | None:
    """创建或复用 LlmConfig 对应的客户端。

    Args:
        config: LlmConfig（或兼容鸭子类型），需含 id、provider、model_name、api_endpoint、api_key_encrypted；
                传 None 表示无配置（走 OPENAI provider + 环境变量 key）。
        settings: 可选 Settings，默认取单例
        apiKey: 可选明文 key（测试注入）；否则按 provider 从配置密文或环境变量解析
    """
    settings = settings or getSettings()
    configId = _resolveConfigId(config)
    if configId in _clients:
        return _clients[configId]

    # config=None 时默认走 OPENAI provider，从 apiKey 或 settings.openaiApiKey 取 key
    if config is None or not hasattr(config, "provider"):
        provider = ProviderType.OPENAI
        key = apiKey or settings.openaiApiKey
        if not key:
            # 无有效 key → 返回 None，调用方应处理（如 503 LLMUnavailableError）
            return None
        anon_config = type("AnonLlmConfig", (), {
            "id": configId,
            "provider": provider.value,
            "model_name": getattr(settings, "openaiModel", "gpt-4o-mini"),
            "api_endpoint": None,
            "api_key_encrypted": None,
        })()
        client: BaseLlmClient = OpenAiClient(
            anon_config,
            apiKey=key,
            provider=provider,
        )
        _clients[configId] = client
        return client

    provider = ProviderType(config.provider)
    if provider == ProviderType.OLLAMA:
        baseUrl = getattr(config, "api_endpoint", None) or settings.ollamaBaseUrl
        client: BaseLlmClient = OllamaClient(config, baseUrl=baseUrl)
    else:
        # 关键修复：先解析 key，为空时主动 return None 而非让 OpenAI SDK 构造时报
        # OpenAIError("Missing credentials") — 后者会被 FastAPI 当 500 处理。
        # 返回 None 让调用方走「未配置 LLM → 503」语义路径。
        key = apiKey or _resolveApiKey(config, provider, settings)
        if not key:
            logger.warning(
                "createClient: config id=%s provider=%s model=%s 无可用 API key "
                "（config.api_key_encrypted 为空 且 settings.%sApiKey 也为空）",
                getattr(config, "id", None), provider.value,
                getattr(config, "model_name", None), provider.value.lower(),
            )
            return None
        client = OpenAiClient(config, apiKey=key, provider=provider)
        # audit-fx-1.1：生产环境阻断非 https / 私有 IP endpoint
        if getattr(config, "api_endpoint", None):
            validateEndpointUrl(config.api_endpoint)
    _clients[configId] = client
    return client


def _resolveConfigId(config: Any) -> int:
    configId = getattr(config, "id", None)
    if configId is None:
        # 未持久化对象（测试用），用 Python 对象 id
        return id(config)
    return int(configId)


def _resolveApiKey(config: Any, provider: ProviderType, settings: Settings) -> str:
    """优先解密配置中的密文 key，否则按 provider 回退到环境变量。"""
    encrypted = getattr(config, "api_key_encrypted", None)
    if encrypted:
        return decryptApiKey(encrypted)
    if provider == ProviderType.OPENAI:
        return settings.openaiApiKey
    if provider == ProviderType.AZURE_OPENAI:
        return settings.azureOpenaiApiKey
    if provider == ProviderType.MOONSHOT:
        # Moonshot(Kimi) 的 key 存 MOONSHOT_API_KEY 环境变量
        return getattr(settings, "moonshotApiKey", "") or ""
    if provider == ProviderType.OPENAI_COMPATIBLE_PROXY:
        # 按模型名启发：deepseek/qwen/moonshot
        name = (config.model_name or "").lower()
        if "deepseek" in name:
            return settings.deepseekApiKey
        if "qwen" in name:
            return settings.qwenApiKey
        if "kimi" in name or "moonshot" in name:
            return getattr(settings, "moonshotApiKey", "") or ""
        return settings.openaiApiKey
    return ""


async def invalidateClient(configId: int) -> None:
    """使指定配置的缓存客户端失效（配置更新/停用后必须调用）。

    OpenAiClient 构造时固化 model_name/api_endpoint/key（见其 __init__ 注释），
    若不从缓存移除，运行中的进程将永远使用编辑前的旧配置。
    close 失败不能让旧客户端留在缓存里——失效语义优先于优雅关闭。
    """
    client = _clients.pop(configId, None)
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        logger.warning("invalidateClient: 关闭旧客户端失败 configId=%s", configId, exc_info=True)


def resetFactory() -> None:
    """清空客户端缓存（测试用；不关闭连接，因测试注入的是 mock）。"""
    _clients.clear()
