"""OpenAI 兼容 embedding 客户端。

调用 /v1/embeddings 生成文本向量。复用 openai SDK 的 AsyncOpenAI，
与 chat/completions 同一套 base_url / api_key 配置模式（DeepSeek/Qwen 等国内代理亦兼容）。

构造不抛错（未配置 key 时延迟到首次调用报错），保证未启用 embedding 时系统可正常启动。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.config import Settings, getSettings
from app.domain.error_messages import (
    MSG_EMBEDDING_MISSING_API_KEY,
    MSG_EMBEDDING_MISSING_API_KEY_DETAIL,
    MSG_LLM_CALL_FAILED,
)
from app.domain.exceptions import LlmClientError
from app.infrastructure.llm.http_client import buildHttpClient

logger = logging.getLogger(__name__)

# openai SDK 拒绝空 api_key（"Missing credentials"）。本地免鉴权服务（Ollama / oMLX）
# 无真实 key：显式配置了 base_url 时用占位 key 满足 SDK 校验，请求体仍带该占位 token，
# 本地服务会忽略 Authorization 头；远端需鉴权端点缺失 key 时返回 401 并包装为调用错误。
_KEYLESS_PLACEHOLDER = "not-needed"

# 状态探测（checkHealth）的 HTTP 超时（秒）
_HEALTH_TIMEOUT_SECONDS = 5


def _applyHostOverride(baseUrl: str, override: str) -> str:
    """把 base_url 中的 loopback 主机改写为部署级覆盖主机（不可变，返回新串）。

    Docker 容器内 localhost 指向容器自身，宿主机上运行的 Ollama / oMLX 需经
    host.docker.internal 访问。provider seed 与 env 的 base_url 均以 localhost 作规范
    写法，部署时经 EMBEDDING_HOST_OVERRIDE 覆盖主机名即可，无需改库或 CRUD。非
    loopback 主机（真实远端端点）不改写；override 为空时原样返回。
    """
    if not override or not baseUrl:
        return baseUrl
    try:
        parts = urlsplit(baseUrl)
    except ValueError:
        return baseUrl
    host = parts.hostname
    if host not in ("localhost", "127.0.0.1"):
        return baseUrl
    return urlunsplit(parts._replace(netloc=_replaceHost(parts.netloc, host, override)))


def _replaceHost(netloc: str, host: str, override: str) -> str:
    """把 netloc 中第一个（大小写不敏感）主机名替换为 override，其余部分原样保留。

    urlsplit 的 hostname 已小写化；直接 str.replace 对 LOCALHOST 这类大写写法会静默
    失败，故按小写定位后在原串上替换，避免丢掉端口/路径等。仅当 host 已确认是
    loopback 时调用。
    """
    idx = netloc.lower().find(host)
    if idx < 0:
        return netloc
    return netloc[:idx] + override + netloc[idx + len(host) :]


class EmbeddingClient:
    """OpenAI 兼容 /v1/embeddings 客户端。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        apiBase: str | None = None,
        apiKey: str | None = None,
        client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        settings = settings or getSettings()
        self._model = model or settings.embeddingModel
        # embedding 专属配置优先；未配置时回退到主 LLM（OpenAI 兼容）的 base_url / api_key，
        # 使语义搜索在仅配置 OPENAI_API_KEY 时开箱即用（与 factory._resolveApiKey 的回退同构）。
        self._apiBase = apiBase if apiBase is not None else (settings.embeddingApiBase or settings.openaiBaseUrl)
        # 部署级主机覆盖（Docker host.docker.internal）：显式 apiBase 与 env 回退
        # 两条路径一致改写 loopback 主机。getattr 兜底测试注入的假 Settings。
        self._apiBase = _applyHostOverride(
            self._apiBase, getattr(settings, "embeddingHostOverride", "")
        )
        self._apiKey = apiKey if apiKey is not None else (settings.embeddingApiKey or settings.openaiApiKey)
        self._client = client  # 测试可注入；否则延迟到首次 embed 构造

    @property
    def apiBase(self) -> str:
        """解析后的 embedding 端点 base_url（供状态看板展示/判断是否已配置）。"""
        return self._apiBase

    async def checkHealth(self) -> None:
        """轻量连通性探测：GET {base_url}/models。

        收到任意 HTTP 响应即视为服务可达（仅网络错误/超时抛异常）；未配置
        base_url 时抛 LlmClientError。复用 buildHttpClient 绕过系统代理。
        """
        if not self._apiBase:
            raise LlmClientError(
                MSG_EMBEDDING_MISSING_API_KEY,
                detail=MSG_EMBEDDING_MISSING_API_KEY_DETAIL,
            )
        url = self._apiBase.rstrip("/") + "/models"
        async with buildHttpClient() as http:
            await http.get(url, timeout=_HEALTH_TIMEOUT_SECONDS)

    def _ensureClient(self) -> Any:
        """按需构造底层 SDK 客户端（连接缓存，等价于 LLM factory 的单例缓存）。"""
        if self._client is None:
            # 完全未配置（无 key 且无 base_url）才报"缺少 API key"；显式配置了 base_url 的
            # 本地服务（Ollama / oMLX）无需鉴权，允许空 key 直接构造。
            if not self._apiKey and not self._apiBase:
                raise LlmClientError(
                    MSG_EMBEDDING_MISSING_API_KEY,
                    detail=MSG_EMBEDDING_MISSING_API_KEY_DETAIL,
                )
            from openai import AsyncOpenAI

            # 注入 trust_env=False 的 httpx client：绕过 macOS 系统代理（127.0.0.1:12450）
            # 劫持，否则对 localhost/Ollama 的 embedding 调用会间歇性 502（见 http_client.py）。
            self._client = AsyncOpenAI(
                api_key=self._apiKey or _KEYLESS_PLACEHOLDER,
                base_url=self._apiBase or None,
                http_client=buildHttpClient(),
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量生成文本向量，返回顺序与输入一致。"""
        try:
            response = await self._ensureClient().embeddings.create(
                model=self._model, input=texts
            )
        except LlmClientError:
            raise
        except Exception as exc:
            raise LlmClientError(
                MSG_LLM_CALL_FAILED.format(provider="Embedding", exc=exc),
                detail=str(exc),
            ) from exc

        data = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in data]

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
