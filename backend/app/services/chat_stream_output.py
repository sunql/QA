"""流式回答输出：块间超时保护 + 主模型失败降级（SSE 的 answer 阶段）。

ChatService 以 mixin 方式组合本模块（class ChatService(ChatStreamOutputMixin)），
提供：
- `_iterStreamChunks`：逐块读取流式响应，块间超时保护，防止 LLM 挂起占用连接。
- `_streamAnswerWithFallback`：主模型失败且未产出 token 时降级到最便宜可用模型。
- `_runL1AndL4Preamble`：L1 KPI + L4 Agent Loop 预检，普通与流式入口共用。

mixin 契约（依赖 ChatService 提供的实例成员）：
- `self._llmFactory(config)`：按配置创建 LLM 客户端。
- `self._modelRouter.selectFallbackModel(configs, excludeId)`：选择降级模型。
- `self._recordUsage(...)`：记录 Token 用量审计行。
- `self._buildAnswerPrompt(question, sql, data)`：构建回答 user prompt。
- `self._kpiMatcher`：KpiSemanticMatchService 实例。
- `self._buildL1Response(match, session)`：L1 命中时构建回答。
- `self._resolveKpiCatalog(code, session)`：查 KpiCatalog。
- `self._handleNl2SqlAgent(session, dto, user)`：L4 Agent Loop。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig
from app.domain.schemas import ChatRequest
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage, StreamChunk
from app.services.chart_thresholds import loadFullDataThreshold
from app.services.chat_constants import (
    USAGE_PURPOSE_ANSWER_STREAM_FAILED,
    USAGE_PURPOSE_FALLBACK_ANSWER,
)
from app.domain.error_messages import (
    MSG_INTERNAL_ERROR_GENERIC,
    MSG_KPI_DATASOURCE_MISMATCH,
)

logger = logging.getLogger(__name__)

_STREAM_CHUNK_TIMEOUT_SECONDS = 45.0  # 回答流块间超时：防止 LLM 挂起占用连接/会话（4-3：120s→45s）
from app.services.prompts_zh import PROMPT_ANSWER_SYSTEM as _ANSWER_SYSTEM_PROMPT


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
        - 主模型失败且未产出 token → 降级，记录 purpose=USAGE_PURPOSE_FALLBACK_ANSWER
        - 已产出 token 后中断（无法回退）→ 记录 purpose=USAGE_PURPOSE_ANSWER_STREAM_FAILED 后上抛
        逐块读取经 _iterStreamChunks 块间超时保护，防止 LLM 挂起占用连接。
        """
        # Task 2：FULL_DATA_THRESHOLD 迁 system_config 后，持 session 的调用方现读。
        full_data_threshold = await loadFullDataThreshold(session)
        messages = [
            LlmMessage(role="system", content=_ANSWER_SYSTEM_PROMPT),
            LlmMessage(
                role="user",
                content=self._buildAnswerPrompt(
                    dto.question, sql, data, history=history,
                    full_data_threshold=full_data_threshold,
                ),
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
                        session, sessionId, attempt, 0, 0, purpose=USAGE_PURPOSE_ANSWER_STREAM_FAILED,
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
                    session, sessionId, attempt, 0, 0, purpose=USAGE_PURPOSE_FALLBACK_ANSWER,
                )
                attempt = fallback


# ===========================================================================
# L1 KPI + L4 Agent pre-amble（普通与流式共用）
# ===========================================================================


class PreambleResult(BaseModel):
    """L1/L4 命中后的封装；含生成 SSE 帧所需全部字段。"""

    routing_layer: Literal["L1", "L4"]
    answer: str
    chart_type: Any = None  # ChartType | None — avoid import cycle
    chart_option: dict | None = None
    visual_rationale: dict | str | None = None
    # L1 KPI 命中信息（用于 ChatResponse kpi_code / kpi_name / confidence 字段）
    kpi_code: str | None = None
    kpi_name: str | None = None
    confidence: float | None = None  # KpiMatchResult.confidence
    # L4
    agent_run: dict | None = None
    # L1 cross-datasource mismatch
    datasource_mismatch: bool = False
    mismatch_warning: str | None = None
    # 完整 ChatResponse（非流式入口需要完整响应；流式入口只用上面那些字段）。
    # 用 Any 破循环依赖（同 chart_type 的既有做法）：本模块在 ChatResponse 之前导入，
    # 若写真实类型注解，pydantic 在建类时会求值该注解（即使有 from __future__ annotations
    # 也只在 type annotation 层面延迟，BaseModel.model_fields 的求值仍需运行时解析），
    # 导致 NameError / PydanticUndefinedAnnotation；用 TYPE_CHECKING + 字符串字面量
    # 也同样会在 pydantic 求值时失败（pydantic 不走 __annotations__ 的 TYPE_CHECKING 分支）。
    # 唯一可行方案是 TypeVar + model_rebuild()，代价远大于收益；保持 Any 并记录原因。
    full_response: Any = None


def requireFullResponse(preamble: PreambleResult, routing_layer: str) -> Any:
    """守卫：full_response 未填充则快速失败。

    这是内部不变量检查（代码 bug，不是用户输入），所以：
    - 用 RuntimeError（不是 DomainError）→ 落到 500 兜底 handler，
      不走 DomainError 那条把 message 原样写进响应体的路径；
    - 文案取 error_messages 常量，不在 services 层硬编码，且不含任何内部标识；
    - 详细诊断（routing_layer、preamble 各字段）只进日志。
    """
    if preamble.full_response is not None:
        return preamble.full_response
    logger.error(
        "PreambleResult invariant broken: full_response is None for routing_layer=%s "
        "(preamble fields: routing_layer=%r, answer=%r, chart_type=%r). "
        "This indicates a code bug: a PreambleResult construction point failed to "
        "populate full_response. Check L1L4PreambleMixin._runL1AndL4Preamble.",
        routing_layer,
        preamble.routing_layer,
        preamble.answer,
        preamble.chart_type,
    )
    raise RuntimeError(MSG_INTERNAL_ERROR_GENERIC)


class L1L4PreambleMixin:
    """抽 KPI/L4 预检为共享 mixin，让普通 + 流式入口共用同一条路径。"""

    # Subclasses must provide _kpiMatcher (KpiSemanticMatchService)
    _kpiMatcher: Any

    async def _runL1AndL4Preamble(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        user: CurrentUser | None,
    ) -> PreambleResult | None:
        """KPI 优先（命中短路）；未命中则尝试 L4；仍未命中返 None（让主路径继续）。

        行为对 source-provider 改：
        - KPI 用 dto.datasourceId 比较 feat.datasource_id（真 FK 列，非 data_source text）；
          不匹配给 mismatch_warning（不静默）
        - L4 不改行为
        """
        # 1. KPI 匹配（与 chat_service.py:409-432 同款 try/except 降级）
        try:
            match = await self._kpiMatcher.match(dto.question)
        except Exception:  # noqa: BLE001 — 降级到原 LLM 流水线
            logger.warning("L1 KPI match failed, falling back to LLM", exc_info=True)
            match = None

        if match is not None:
            l1 = await self._buildL1Response(match, session)
            if l1 is not None:
                # P0 合并项 6：检查 datasource 是否匹配（仅在 KPI 实际绑定了数据源时）
                mismatch = False
                warning = None
                feat = await self._resolveKpiCatalog(match.code, session)
                if feat is not None and feat.datasource_id is not None and feat.datasource_id != dto.datasourceId:
                    mismatch = True
                    warning = MSG_KPI_DATASOURCE_MISMATCH.format(
                        datasource_id=feat.datasource_id,
                        user_datasource_id=dto.datasourceId,
                    )
                return PreambleResult(
                    routing_layer="L1",
                    answer=l1.answer,
                    chart_type=l1.chartType,
                    chart_option=l1.chartOption,
                    visual_rationale=l1.visual_rationale,
                    kpi_code=match.code,
                    kpi_name=l1.kpi_name,
                    confidence=match.confidence,
                    datasource_mismatch=mismatch,
                    mismatch_warning=warning,
                    full_response=l1,
                )

        # 2. L4 Agent Loop
        l4 = await self._handleNl2SqlAgent(session, dto, user=user)
        if l4 is not None:
            return PreambleResult(
                routing_layer="L4",
                answer=l4.answer,
                agent_run=(
                    l4.agent_run.model_dump(mode="json", by_alias=True)
                    if l4.agent_run is not None else None
                ),
                full_response=l4,
            )

        return None


__all__ = [
    "ChatStreamOutputMixin",
    "L1L4PreambleMixin",
    "PreambleResult",
    "_STREAM_CHUNK_TIMEOUT_SECONDS",
    "_ANSWER_SYSTEM_PROMPT",
]
