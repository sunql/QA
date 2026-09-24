"""OpenAI 兼容客户端。

同时覆盖 OpenAI、Azure OpenAI、国内 OpenAI 兼容代理（DeepSeek/通义/Qwen 等）。
三者均通过 openai SDK 调用，仅初始化参数不同：
- OpenAI / 代理：AsyncOpenAI(api_key, base_url?)
- Azure：AsyncAzureOpenAI(api_key, azure_endpoint, api_version)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.config import getSettings
from app.domain.enums import ProviderType
from app.domain.error_messages import (
    MSG_AZURE_OPENAI_MISSING_ENDPOINT,
    MSG_AZURE_OPENAI_MISSING_ENDPOINT_DETAIL,
    MSG_LLM_CALL_FAILED,
    MSG_LLM_STREAM_FAILED,
)
from app.domain.exceptions import LlmClientError
from app.infrastructure.llm.base_client import (
    BaseLlmClient,
    LlmMessage,
    LlmResponse,
    LlmResponseWithTools,
    StreamChunk,
    ToolCall,
)
from app.infrastructure.llm.factory import acquire_llm_concurrency

logger = logging.getLogger(__name__)


class OpenAiClient(BaseLlmClient):
    """OpenAI / Azure / 兼容代理统一客户端。"""

    def __init__(
        self,
        config: Any,
        *,
        apiKey: str,
        provider: ProviderType = ProviderType.OPENAI,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        # 快照标量属性：客户端被 createClient 按 config_id 跨请求缓存复用，而 config 是
        # ORM 实例。若前一请求会话以回滚结束（getDb 的 except: rollback），该 config 会
        # 过期且脱离，调用期再懒加载会抛 DetachedInstanceError（被包装为通用"服务内部
        # 错误"）。故在构造时固化 model_name/api_endpoint，调用期不再触碰 ORM。
        self._modelName = getattr(config, "model_name", None)
        self._apiEndpoint = getattr(config, "api_endpoint", None)
        # client 可注入以利测试；否则按 provider 构造真实 SDK 客户端
        self._client = client if client is not None else self._buildRealClient(apiKey)

    def _buildRealClient(self, apiKey: str) -> Any:
        from openai import AsyncAzureOpenAI, AsyncOpenAI

        from app.infrastructure.llm.http_client import buildHttpClient

        # 注入 trust_env=False 的 httpx client：绕过 macOS 系统代理（127.0.0.1:12450）
        # 劫持，否则 OpenAI/DeepSeek/Ollama 等调用会间歇性 502（见 http_client.py）。
        common: dict[str, Any] = {"http_client": buildHttpClient()}
        settings = getSettings()
        if self._provider == ProviderType.AZURE_OPENAI:
            endpoint = self._apiEndpoint or settings.azureOpenaiEndpoint
            if not endpoint:
                raise LlmClientError(
                    MSG_AZURE_OPENAI_MISSING_ENDPOINT,
                    provider=self._provider.value,
                    detail=MSG_AZURE_OPENAI_MISSING_ENDPOINT_DETAIL,
                )
            return AsyncAzureOpenAI(
                api_key=apiKey,
                azure_endpoint=endpoint,
                api_version=settings.azureOpenaiApiVersion,
                **common,
            )
        kwargs: dict[str, Any] = {"api_key": apiKey, **common}
        if self._apiEndpoint:
            kwargs["base_url"] = self._apiEndpoint
        return AsyncOpenAI(**kwargs)

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        payload: dict[str, Any] = {
            "model": model or self._modelName,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if temperature is not None:
            # Moonshot K3 要求 temperature=1，不接受其他值；强置避免 400
            if self._provider == ProviderType.MOONSHOT and temperature != 1.0:
                temperature = 1.0
            payload["temperature"] = temperature
        if maxTokens is not None:
            payload["max_tokens"] = maxTokens
        payload.update(kwargs)

        try:
            async with acquire_llm_concurrency():
                response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            raise LlmClientError(
                MSG_LLM_CALL_FAILED.format(provider=self._provider.value, exc=exc),
                provider=self._provider.value,
                detail=str(exc),
            ) from exc

        usage = getattr(response, "usage", None)
        promptTokens = getattr(usage, "prompt_tokens", 0) or 0
        completionTokens = getattr(usage, "completion_tokens", 0) or 0
        content = ""
        choices = getattr(response, "choices", None)
        if choices:
            content = getattr(choices[0].message, "content", "") or ""
        return LlmResponse(
            content=content,
            # 用快照而非 self._config.model_name：避免 getattr 的 default 参数在调用期
            # 触达可能已过期脱离的 ORM 实例（DetachedInstanceError）
            modelName=getattr(response, "model", self._modelName),
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
        """流式补全。用 stream_options.include_usage 在末块获取真实 token 统计。

        部分兼容代理不支持 include_usage（不返回 usage 块）时，末块 token 记为 0，
        与 complete() 的近似计量语义一致。
        """
        payload: dict[str, Any] = {
            "model": model or self._modelName,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            # Moonshot K3 要求 temperature=1，不接受其他值；强置避免 400
            if self._provider == ProviderType.MOONSHOT and temperature != 1.0:
                temperature = 1.0
            payload["temperature"] = temperature
        if maxTokens is not None:
            payload["max_tokens"] = maxTokens
        payload.update(kwargs)

        promptTokens = 0
        completionTokens = 0
        modelName = self._modelName
        try:
            async with acquire_llm_concurrency():
                stream = await self._client.chat.completions.create(**payload)
                async for chunk in stream:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        promptTokens = getattr(usage, "prompt_tokens", 0) or 0
                        completionTokens = getattr(usage, "completion_tokens", 0) or 0
                        continue
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    delta = getattr(choices[0].delta, "content", None)
                    if delta:
                        modelName = getattr(chunk, "model", modelName) or modelName
                        yield StreamChunk(
                            content=delta, isDone=False, promptTokens=0, completionTokens=0, modelName=modelName
                        )
                yield StreamChunk(
                    content="",
                    isDone=True,
                    promptTokens=promptTokens,
                    completionTokens=completionTokens,
                    modelName=modelName,
                )
        except Exception as exc:
            raise LlmClientError(
                MSG_LLM_STREAM_FAILED.format(provider=self._provider.value, exc=exc),
                provider=self._provider.value,
                detail=str(exc),
            ) from exc

    async def complete_with_tools(
        self,
        messages: list[LlmMessage],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
    ) -> LlmResponseWithTools:
        """支持 tool calling 的 completion，透传 OpenAI tools API。"""
        # 序列化消息：tool result 必须带 tool_call_id，assistant 触发了 tool calling
        # 时必须回传 tool_calls（否则 provider 无法关联 tool_call_id ↔ 调用）。
        # 历史 bug：仅传 {role, content} 导致深求/多轮 tool 调用 400 'missing field tool_call_id'。
        # 另：tool_calls 必须是 OpenAI 形状 {id, type:'function', function:{name, arguments(JSON 字符串)}}；
        # 上游 agent_runtime 可能给 langchain 形状 {id, name, args}，需归一化。
        def _normalize_tool_calls(raw_calls):
            out = []
            for tc in raw_calls:
                # 已是 OpenAI 形状（带 function 键）
                if isinstance(tc, dict) and "function" in tc:
                    out.append({
                        "id": tc["id"],
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": tc["function"].get("name") if isinstance(tc["function"], dict) else tc.get("name"),
                            "arguments": (
                                tc["function"]["arguments"]
                                if isinstance(tc["function"], dict) and "arguments" in tc["function"]
                                else json.dumps(tc.get("args", {}), ensure_ascii=False)
                            ),
                        },
                    })
                else:
                    # langchain 形状 {id, name, args}
                    args = tc.get("args", {}) if isinstance(tc, dict) else {}
                    name = tc.get("name") if isinstance(tc, dict) else None
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    out.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    })
            return out

        serialized: list[dict[str, Any]] = []
        for m in messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.role == "tool" and m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
                if m.name:
                    item["name"] = m.name
            elif m.role == "assistant" and m.tool_calls:
                item["tool_calls"] = _normalize_tool_calls(m.tool_calls)
            serialized.append(item)
        payload: dict[str, Any] = {
            "model": self._modelName,
            "messages": serialized,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        try:
            async with acquire_llm_concurrency():
                response = await self._client.chat.completions.create(**payload)
        except Exception as exc:
            raise LlmClientError(
                MSG_LLM_CALL_FAILED.format(provider=self._provider.value, exc=exc),
                provider=self._provider.value,
                detail=str(exc),
            ) from exc

        tool_calls: list[ToolCall] = []
        raw_message = response.choices[0].message
        if raw_message.tool_calls:
            for raw_tc in raw_message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=raw_tc.id,
                        name=raw_tc.function.name,
                        args=json.loads(raw_tc.function.arguments),
                    )
                )

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        return LlmResponseWithTools(
            content=getattr(raw_message, "content", None),
            tool_calls=tool_calls,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            model=getattr(response, "model", self._modelName) or self._modelName,
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
