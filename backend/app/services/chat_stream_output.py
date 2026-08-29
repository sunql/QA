"""流式回答输出：块间超时保护 + 主模型失败降级（SSE 的 answer 阶段）。

ChatService 以 mixin 方式组合本模块（class ChatService(ChatStreamOutputMixin)），
提供：
- `_iterStreamChunks`：逐块读取流式响应，块间超时保护，防止 LLM 挂起占用连接。
- `_streamAnswerWithFallback`：主模型失败且未产出 token 时降级到最便宜可用模型。

mixin 契约（依赖 ChatService 提供的实例成员）：
- `self._llmFactory(config)`：按配置创建 LLM 客户端。
- `self._modelRouter.selectFallbackModel(configs, excludeId)`：选择降级模型。
- `self._recordUsage(...)`：记录 Token 用量审计行。
- `self._buildAnswerPrompt(question, sql, data)`：构建回答 user prompt。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig
from app.domain.schemas import ChatRequest
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage, StreamChunk

logger = logging.getLogger(__name__)

_STREAM_CHUNK_TIMEOUT_SECONDS = 45.0  # 回答流块间超时：防止 LLM 挂起占用连接/会话（4-3：120s→45s）
_ANSWER_SYSTEM_PROMPT = (
    "你是一名企业数据分析助手。根据查询结果用简洁的中文回答用户问题，"
    "不要编造数据，不要输出 SQL。"
    "**禁止反向追问**：不要询问用户'需要继续查询吗 / 是否需要进一步分析 / "
    "还需要看其他吗'。"
    "如查询结果不足以回答问题，直接说明当前结果能回答什么、不能回答什么即可。"
)


class ChatStreamOutputMixin:
    """流式回答输出的公共能力（mixin，被 ChatService 组合）。"""

    async def _iterStreamChunks(
        self, client: BaseLlmClient, messages: list[LlmMessage], model: str
    ) -> AsyncIterator[StreamChunk]:
        """逐块读取流式响应，块间超时保护。

        LLM 挂起（长时间无新块）时按超时中止并转为 LlmClientError，
        保证 SSE 不会无限期占用连接与数据库会话。
        """
        generator = client.completeStream(messages=messages, model=model)
        while True:
            try:
                chunk = await asyncio.wait_for(
                    generator.__anext__(), timeout=_STREAM_CHUNK_TIMEOUT_SECONDS,
                )
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise LlmClientError(
                    f"回答流超过 {_STREAM_CHUNK_TIMEOUT_SECONDS}s 无新块，触发超时中止",
                    provider=model,
                ) from exc
            yield chunk

    async def _streamAnswerWithFallback(
        self,
        session: AsyncSession,
        sessionId: str,
        configs: list[LlmConfig],
        primary: LlmConfig,
        dto: ChatRequest,
        sql: str,
        data: list[dict],
        forced: bool = False,
        history: str = "",
    ) -> AsyncIterator[tuple[StreamChunk, LlmConfig, tuple[int, int]]]:
        """流式回答：主模型失败且未产出任何 token 时降级到最便宜可用模型。

        history 为最近对话历史（1-5，可为空串），随回答 prompt 一并注入，
        支持跨轮连贯与对比。与降级语义无关，仅在构造 messages 时使用。

        降级在首次失败时才惰性求值（与 _callWithFallback 语义一致，避免无谓查询）。
        产出 (chunk, 实际服务模型, (0,0)) 三元组；第三元恒为 (0,0)——流式失败时
        客户端不会回传已消耗 token（done 块未到达），浪费 token 不可计量，仅以零
        token 审计行作为失败标记：
        - 主模型失败且未产出 token → 降级，记录 purpose="fallback_answer"
        - 已产出 token 后中断（无法回退）→ 记录 purpose="answer_stream_failed" 后上抛
        逐块读取经 _iterStreamChunks 块间超时保护，防止 LLM 挂起占用连接。
        """
        messages = [
            LlmMessage(role="system", content=_ANSWER_SYSTEM_PROMPT),
            LlmMessage(
                role="user",
                content=self._buildAnswerPrompt(dto.question, sql, data, history=history),
            ),
        ]
        emittedContent = False
        attempt = primary
        while True:
            try:
                async for chunk in self._iterStreamChunks(
                    self._llmFactory(attempt), messages, attempt.model_name,
                ):
                    if chunk.content:
                        emittedContent = True
                    yield chunk, attempt, (0, 0)
                return
            except LlmClientError as exc:
                if emittedContent:
                    # 已产出 token，客户端已收到部分内容，无法回退；记失败标记审计行
                    await self._recordUsage(
                        session, sessionId, attempt, 0, 0, purpose="answer_stream_failed",
                    )
                    logger.warning("回答流中断（已产出 token，不再降级）: %s", exc.message)
                    raise
                if attempt is not primary or forced:
                    raise
                fallback = self._modelRouter.selectFallbackModel(configs, primary.id)
                if fallback is None:
                    raise
                logger.warning("回答流模型 %s 失败，尝试降级: %s", attempt.model_name, exc.message)
                await self._recordUsage(
                    session, sessionId, attempt, 0, 0, purpose="fallback_answer",
                )
                attempt = fallback


__all__ = [
    "ChatStreamOutputMixin",
    "_STREAM_CHUNK_TIMEOUT_SECONDS",
    "_ANSWER_SYSTEM_PROMPT",
]
