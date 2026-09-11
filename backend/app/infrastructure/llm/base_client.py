"""LLM 客户端抽象层。

定义统一的消息、响应与客户端接口，屏蔽不同 provider 的调用差异。
所有响应/消息为不可变 dataclass。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ToolCall:
    """OpenAI tool-call 节点。"""

    id: str
    name: str
    args: dict  # JSON-decoded arguments


@dataclass(frozen=True)
class LlmResponseWithTools:
    """支持 tool calling 的 LLM 调用结果。"""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None
    model: str = ""


@dataclass(frozen=True)
class LlmMessage:
    """对话消息。"""

    role: str
    content: str
    # tool_call_id: 当 role='tool' 时必填（OpenAI tool API 要求 tool result message
    # 引用前一条 assistant 消息的 tool_calls[i].id；缺失会导致 deepseek/openai 400）
    tool_call_id: str | None = None
    # tool_calls: 当 role='assistant' 且本轮触发了 tool calling 时携带（list[dict]）
    tool_calls: tuple[dict, ...] | None = None
    # name: 当 role='tool' 时可选（部分 provider 要求）
    name: str | None = None


@dataclass(frozen=True)
class LlmResponse:
    """LLM 调用结果。"""

    content: str
    modelName: str
    promptTokens: int
    completionTokens: int
    totalTokens: int

    @property
    def isApproximateUsage(self) -> bool:
        return self.promptTokens == 0 and self.completionTokens == 0


@dataclass(frozen=True)
class StreamChunk:
    """流式补全的单块数据（不可变）。

    content 为本次增量文本（空串表示元信息块）；
    isDone=True 的末块携带本次调用的累计 prompt/completion token。
    """

    content: str
    isDone: bool
    promptTokens: int
    completionTokens: int
    modelName: str


class BaseLlmClient(ABC):
    """LLM 客户端接口。"""

    @abstractmethod
    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        """发起一次补全请求，返回统一响应。"""

    @abstractmethod
    def completeStream(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """发起流式补全，逐块产出 StreamChunk；末块 isDone=True 并携带 token 统计。"""

    @abstractmethod
    async def close(self) -> None:
        """释放底层连接。"""

    @abstractmethod
    async def complete_with_tools(
        self,
        messages: list[LlmMessage],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
    ) -> LlmResponseWithTools:
        """发起支持 tool calling 的补全请求，透传给底层 provider。"""
