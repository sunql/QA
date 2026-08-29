"""Ollama 本地模型客户端。

通过 aiohttp 直接调用 Ollama 的 /api/chat 接口。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from app.domain.error_messages import (
    MSG_LLM_CALL_FAILED,
    MSG_LLM_HTTP_ERROR,
    MSG_LLM_STREAM_FAILED,
    MSG_LLM_STREAM_INVALID_JSON,
)
from app.domain.exceptions import LlmClientError
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage, LlmResponse, StreamChunk

logger = logging.getLogger(__name__)

_OLLAMA_TIMEOUT = aiohttp.ClientTimeout(total=500.0)


class OllamaClient(BaseLlmClient):
    """Ollama 客户端。"""

    def __init__(self, config: Any, *, baseUrl: str, session: aiohttp.ClientSession | None = None) -> None:
        self._config = config
        # 快照 model_name：客户端被 createClient 缓存复用，
        # 调用期不再懒加载可能已过期脱离的 ORM 实例（DetachedInstanceError）
        self._modelName = getattr(config, "model_name", None)
        self._baseUrl = baseUrl.rstrip("/")
        # session 可注入以利测试；使用 aiohttp 而非 httpx 避免 HTTP/2 代理检测问题
        self._session = session

    async def _getSession(self) -> aiohttp.ClientSession:
        if self._session is None:
            # connector 限制同一 host 最大并发连接数，避免 Ollama 爆掉
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=10)
            self._session = aiohttp.ClientSession(
                base_url=self._baseUrl, timeout=_OLLAMA_TIMEOUT, connector=connector,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        modelName = model or self._modelName
        payload: dict[str, Any] = {
            "model": modelName,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if maxTokens is not None:
            options["num_predict"] = maxTokens
        if options:
            payload["options"] = options

        session = await self._getSession()
        try:
            async with session.post("/api/chat", json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise LlmClientError(
                        MSG_LLM_HTTP_ERROR.format(
                            provider="Ollama", status=resp.status, body=text[:200]
                        ),
                        provider="ollama",
                        detail=str(resp.status),
                    )
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise LlmClientError(
                MSG_LLM_CALL_FAILED.format(provider="Ollama", exc=exc),
                provider="ollama",
                detail=str(exc),
            ) from exc

        message = data.get("message") or {}
        content = message.get("content", "") or ""
        promptTokens = data.get("prompt_eval_count", 0) or 0
        completionTokens = data.get("eval_count", 0) or 0
        return LlmResponse(
            content=content,
            modelName=data.get("model", modelName),
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            totalTokens=promptTokens + completionTokens,
        )

    async def completeStream(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """流式补全。逐行解析 NDJSON；done 块携带 prompt_eval_count/eval_count。

        任何解析/连接异常都包装为 LlmClientError，保证服务层模型降级契约；
        流未以 done 块结束时仍产出零 token 的 done 块，保证调用方正常计量与终止。
        """
        modelName = model or self._modelName
        payload: dict[str, Any] = {
            "model": modelName,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if maxTokens is not None:
            options["num_predict"] = maxTokens
        if options:
            payload["options"] = options

        session = await self._getSession()
        try:
            async with session.post("/api/chat", json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise LlmClientError(
                        MSG_LLM_HTTP_ERROR.format(
                            provider="Ollama", status=resp.status, body=text[:200]
                        ),
                        provider="ollama",
                        detail=str(resp.status),
                    )
                # 用 readline() 而非直接迭代 resp.content：aiohttp StreamReader 的 __aiter__
                # 产出原始字节块（不按换行切分），遇到半行/多行合并会误判非法 NDJSON。
                # readline() 在 aiohttp 内部缓冲，保证每行完整后再交给 JSON 解析。
                while True:
                    line = await resp.content.readline()
                    if not line:
                        break
                    text = line.decode("utf-8").strip()
                    if not text:
                        continue
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise LlmClientError(
                            MSG_LLM_STREAM_INVALID_JSON.format(
                                provider="Ollama", line=text[:200]
                            ),
                            provider="ollama",
                            detail=str(exc),
                        ) from exc
                    message = data.get("message") or {}
                    delta = message.get("content", "") or ""
                    if delta:
                        yield StreamChunk(
                            content=delta, isDone=False, promptTokens=0, completionTokens=0,
                            modelName=modelName,
                        )
                    if data.get("done"):
                        promptTokens = data.get("prompt_eval_count", 0) or 0
                        completionTokens = data.get("eval_count", 0) or 0
                        yield StreamChunk(
                            content="", isDone=True, promptTokens=promptTokens,
                            completionTokens=completionTokens, modelName=data.get("model", modelName),
                        )
                        return
                # 流未以 done 块结束（网络中断/异常服务端）：仍产出 done 块（token 记 0）
                yield StreamChunk(
                    content="", isDone=True, promptTokens=0, completionTokens=0, modelName=modelName,
                )
        except aiohttp.ClientError as exc:
            raise LlmClientError(
                MSG_LLM_STREAM_FAILED.format(provider="Ollama", exc=exc),
                provider="ollama",
                detail=str(exc),
            ) from exc
        except Exception as exc:
            # 非 aiohttp 异常（解析/结构异常等）也必须包装，避免绕过降级契约
            raise LlmClientError(
                MSG_LLM_STREAM_FAILED.format(provider="Ollama", exc=exc),
                provider="ollama",
                detail=str(exc),
            ) from exc
