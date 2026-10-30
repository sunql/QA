"""流式捷径分支（ChatService 的 StreamShortcutMixin）。

不涉及 SQL/计划的轻量流式事件序列：
- `_streamChitchat` / `_chitchatStreamEvents`：闲聊问候（4-4 持久化本轮）
- `_streamClarify`：CLARIFY 概念解释（复用非流式 handler）
- `_streamInterceptCard`：拦截类意图（360°/风险/图推理/Agent 运行）的 SSE
  包装，把 ChatResponse 转 token(整段) + done(携带卡片对象)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import IntentType
from app.domain.schemas import ChatRequest
from app.services.intent_service import IntentResult
from app.services.stream_events import (
    EVENT_DONE,
    EVENT_TOKEN,
    StreamEvent,
)

logger = logging.getLogger(__name__)


class StreamShortcutMixin:
    """流式捷径分支（由 ChatService 组合）。"""

    async def _streamChitchat(
        self, dto: ChatRequest, session: AsyncSession
    ) -> AsyncIterator[StreamEvent]:
        """闲聊流式事件序列（4-4）：先持久化本轮 user+assistant，再产出 token + done。"""
        await self._storeSessionMessages(session, dto.sessionId, dto.question, self._chitchatAnswer(), None)
        for event in self._chitchatStreamEvents():
            yield event

    def _chitchatStreamEvents(self) -> Iterator[StreamEvent]:
        """闲聊的 SSE 事件序列：token(问候) + done(零消耗)。"""
        yield StreamEvent(EVENT_TOKEN, {"content": self._chitchatAnswer()})
        yield StreamEvent(EVENT_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})

    async def _streamClarify(
        self, dto: ChatRequest, session: AsyncSession
    ) -> AsyncIterator[StreamEvent]:
        """CLARIFY 的 SSE 形式：整段概念解释，token 事件 + done（不涉及 SQL）。"""
        pc = await self._buildPipelineContext(
            session, dto, needFewShot=False, needSamples=False, needDrift=False,
        )
        resp = await self._handleClarify(session, dto, pc)
        affinityPayload = (
            {"lockedModel": resp.affinityStatus.lockedModel,
             "remainingTurns": resp.affinityStatus.remainingTurns}
            if resp.affinityStatus is not None
            else None
        )
        yield StreamEvent(EVENT_TOKEN, {"content": resp.answer})
        yield StreamEvent(
            EVENT_DONE,
            {
                "tokensUsed": resp.tokensUsed,
                "cost": resp.cost,
                "modelName": resp.modelName,
                "affinityStatus": affinityPayload,
            },
        )

    async def _streamInterceptCard(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        result: IntentResult,
        user: CurrentUser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """拦截类意图（360°/风险/图推理/Agent 运行）的 SSE 形式。

        #207 审查 HIGH 修复：此前流式路径从不路由这些意图，默认 streaming UI 下
        卡片（Supplier360Card / SupplierRiskCard / GraphTraversalCard / AgentResponseCard）
        从未渲染。此处复用非流式 handler 得到 ChatResponse（保证两条路径语义一致），
        再转成 token(整段 answer) + done(携带卡片对象)。

        卡片对象序列化必须用 model_dump(by_alias=True)：toSse 的 default=str 会把
        Pydantic 模型直接变成 repr 字符串（见 stream_events.StreamEvent.toSse）。
        """
        if result.intent == IntentType.SUPPLIER_360:
            resp = await self._handleSupplier360(session, dto, result)
        elif result.intent == IntentType.SUPPLIER_RISK:
            resp = await self._handleSupplierRisk(session, dto, result)
        elif result.intent == IntentType.GRAPH_REASONING:
            resp = await self._handleGraphReasoning(session, dto, result)
        elif result.intent == IntentType.AGENT_RUN:
            resp = await self._handleAgentRun(session, dto, result, user=user)
        else:
            # 仅拦截意图可达；其他意图由调用方前置守卫（processMessageStream 拦截）。
            raise AssertionError(f"unexpected intercept intent: {result.intent}")

        yield StreamEvent(EVENT_TOKEN, {"content": resp.answer})
        yield StreamEvent(
            EVENT_DONE,
            {
                "tokensUsed": resp.tokensUsed,
                "cost": float(resp.cost),
                "modelName": resp.modelName,
                "agentRun": (
                    resp.agent_run.model_dump(mode="json", by_alias=True)
                    if resp.agent_run is not None
                    else None
                ),
                "supplier360": (
                    resp.supplier360.model_dump(mode="json", by_alias=True)
                    if resp.supplier360 is not None
                    else None
                ),
                "supplierRisk": (
                    resp.supplier_risk.model_dump(mode="json", by_alias=True)
                    if resp.supplier_risk is not None
                    else None
                ),
                "graphTraversal": (
                    resp.graph_traversal.model_dump(mode="json", by_alias=True)
                    if resp.graph_traversal is not None
                    else None
                ),
            },
        )


__all__ = ["StreamShortcutMixin"]
