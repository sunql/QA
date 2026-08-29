"""LLM 客户端抽象层。

定义统一的消息、响应与客户端接口，屏蔽不同 provider 的调用差异。
所有响应/消息为不可变 dataclass。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LlmMessage:
    """对话消息。"""

    role: str
    content: str


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
