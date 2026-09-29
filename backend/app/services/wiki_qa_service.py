"""Wiki Chat 问答服务（feat-wiki-chat）。

编排：加载上 5 轮 wiki_qa 历史 → WikiVectorService 语义检索 → LLM 流式合成
→ wiki_token_usage 计量（mechanism=QA）→ 落库（channel="wiki_qa"）。
无相关 chunks（top1 score < 0.3）走固定模板，零 LLM 调用。

与 doc_qa（rag_qa_service.py）同模式；差异：
- 检索源是 wiki_page_embeddings（WikiVectorService.searchSemantic，embedding
  计量在检索侧已内置）；
- LLM 调用落 wiki_token_usage 台账（核心约束 #3；doc_qa 只在 done 事件报数）。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.models import LlmConfig, SessionMessage
from app.domain.wiki_schemas import WikiChatRequest
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage
from app.services.stream_events import (
    EVENT_QA_CITATIONS,
    EVENT_QA_DONE,
    EVENT_QA_META,
    EVENT_TOKEN,
    StreamEvent,
)
from app.services.wiki_qa_prompt import (
    WIKI_QA_SYSTEM_PROMPT,
    WIKI_QA_USER_TEMPLATE,
    format_chunks_json,
    format_history_block,
)
from app.services.wiki_token_usage_service import WikiTokenUsageService
from app.services.wiki_vector_service import WikiVectorService

logger = logging.getLogger(__name__)

_HISTORY_ROUNDS = 5
_NO_HITS_TEMPLATE = "未在企业 Wiki 中找到相关知识，无法回答该问题。"
_SCORE_THRESHOLD = 0.3
_MECHANISM_QA = "QA"
_PURPOSE_WIKI_CHAT = "wiki_chat_answer"
LlmFactory = Any  # Callable[[LlmConfig], BaseLlmClient]


class WikiQaService:
    """wiki_qa 全流程编排。"""

    def __init__(
        self,
        *,
        wikiVectorService: WikiVectorService | None = None,
        llm_factory: LlmFactory | None = None,
    ) -> None:
        self._wiki_svc = wikiVectorService
        self._llm_factory = llm_factory

    def _ensureWikiSvc(self) -> WikiVectorService:
        if self._wiki_svc is None:
            self._wiki_svc = WikiVectorService()
        return self._wiki_svc

    async def load_history(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        limit: int = _HISTORY_ROUNDS,
    ) -> list[dict[str, str]]:
        """加载上 N 轮 wiki_qa 消息，返回 [{role, content}]，不含 citations。"""
        stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.channel == "wiki_qa",
            )
            .order_by(SessionMessage.created_time.desc())
            .limit(limit * 2)  # 取两倍以凑齐 N 轮 user+assistant
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        out: list[dict[str, str]] = []
        for row in reversed(rows):
            out.append({"role": row.role, "content": row.content})
            if len(out) >= limit * 2:
                break
        return out

    async def _persist(
        self,
        session: AsyncSession,
        dto: WikiChatRequest,
        answer: str,
        citations: list[dict[str, Any]],
        *,
        actor: CurrentUser,
    ) -> None:
        """落库 user 行 + assistant 行（wiki_qa channel）。"""
        session.add(SessionMessage(
            session_id=dto.session_id,
            role="user",
            content=dto.question,
            channel="wiki_qa",
            user_id=actor.userId,
        ))
        session.add(SessionMessage(
            session_id=dto.session_id,
            role="assistant",
            content=answer,
            question=dto.question,
            citations=citations or None,
            channel="wiki_qa",
            user_id=actor.userId,
        ))
        await session.flush()
        await session.commit()

    async def answer_stream(
        self,
        session: AsyncSession,
        dto: WikiChatRequest,
        *,
        actor: CurrentUser,
        configs: list[LlmConfig],
    ) -> AsyncIterator[StreamEvent]:
        """一轮 wiki_qa SSE 事件序列（流式生成器）。

        无相关 hits / top1 score 过低 → meta + citations + token(模板) + done，
        零 LLM 调用；有相关 hits → meta + citations + token×N + done。
        """
        # 1. 加载历史
        history = await self.load_history(session, dto.session_id)
        history_block = format_history_block(history)

        # 2. 语义检索（embedding 计量在 searchSemantic 内已落 wiki_token_usage）
        hits = await self._ensureWikiSvc().searchSemantic(
            session, dto.question, dimension=dto.dimension, topK=dto.top_k,
        )

        # 3. 加 id 给 LLM 引用
        citations = [{"id": idx + 1, **h} for idx, h in enumerate(hits)]

        # 4. 事件先行：meta + citations
        yield StreamEvent(EVENT_QA_META, {"intent": "wiki_qa"})
        yield StreamEvent(EVENT_QA_CITATIONS, {"citations": citations})

        # 5. 短路：无命中或 top1 score 过低 → 固定模板，零 LLM
        if not hits or hits[0].get("score", 0.0) < _SCORE_THRESHOLD:
            yield StreamEvent(EVENT_TOKEN, {"content": _NO_HITS_TEMPLATE})
            yield StreamEvent(EVENT_QA_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})
            await self._persist(session, dto, _NO_HITS_TEMPLATE, citations, actor=actor)
            return

        # 6. 选模型（dto.modelId 优先）
        if dto.model_id is not None:
            selected = next((c for c in configs if c.id == dto.model_id), None)
            # 不存在 / 已停用都视为不可用（与 doc_qa 同口径）
            if selected is None or not getattr(selected, "is_active", True):
                from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE
                raise ValueError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=dto.model_id))
        else:
            from app.services.model_router_service import ModelRouterService, RoutingContext
            router = ModelRouterService()
            selected = router.selectModel(
                configs, dto.question,
                RoutingContext(sessionId=dto.session_id),
            )

        # 7. 拼 prompt（注入 factory 优先；缺省走 createClient——
        #    rag_qa 的「factory 缺省 None」在 prod 直跑必炸，这里修正）
        factory = self._llm_factory
        if factory is None:
            from app.infrastructure.llm.factory import createClient
            factory = createClient
        client = factory(selected)
        if client is None:
            from app.domain.exceptions import LLMUnavailableError
            raise LLMUnavailableError("未配置可用的 LLM，无法进行 Wiki Chat 问答")

        chunks_json = format_chunks_json(hits)
        messages: list[LlmMessage] = [
            LlmMessage(role="system", content=WIKI_QA_SYSTEM_PROMPT.format(chunks_json=chunks_json)),
            LlmMessage(
                role="user",
                content=WIKI_QA_USER_TEMPLATE.format(
                    history_block=history_block, question=dto.question,
                ),
            ),
        ]

        # 8. 流式 LLM
        full_answer = ""
        total_pt = 0
        total_ct = 0
        async for chunk in client.completeStream(messages, model=selected.model_name):
            full_answer += chunk.content
            if chunk.content:
                yield StreamEvent(EVENT_TOKEN, {"content": chunk.content})
            if chunk.isDone:
                total_pt = chunk.promptTokens
                total_ct = chunk.completionTokens

        # 9. 计量 + cost（核心约束 #3：每次 LLM 调用必须记录）
        cost = (
            Decimal(total_pt) * Decimal(str(selected.cost_per_1k_input))
            + Decimal(total_ct) * Decimal(str(selected.cost_per_1k_output))
        ) / Decimal(1000)

        await WikiTokenUsageService().record(
            session,
            mechanism=_MECHANISM_QA,
            modelConfigId=selected.id,
            modelName=selected.model_name,
            promptTokens=total_pt,
            completionTokens=total_ct,
            cost=cost,
            purpose=_PURPOSE_WIKI_CHAT,
        )

        # 10. 落库（user + assistant，同一事务）
        await self._persist(session, dto, full_answer, citations, actor=actor)

        # 11. done
        yield StreamEvent(
            EVENT_QA_DONE,
            {
                "tokensUsed": total_pt + total_ct,
                "cost": float(cost),
                "modelName": selected.model_name,
            },
        )
