"""文档问答 RAG 服务。

编排：加载上 5 轮历史 → Milvus 检索 → LLM 流式合成 → 落库。
无相关 chunks（top1 score < 0.3）走固定模板，零 LLM 调用。
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
from app.domain.schemas import DocQaRequest
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage
from app.services.rag_qa_prompt import (
    DOC_QA_SYSTEM_PROMPT,
    DOC_QA_USER_TEMPLATE,
    format_chunks_json,
    format_history_block,
)
from app.services.rag_service import RagService
from app.services.stream_events import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_QA_CITATIONS,
    EVENT_QA_DONE,
    EVENT_QA_META,
    EVENT_TOKEN,
    StreamEvent,
)

logger = logging.getLogger(__name__)

_HISTORY_ROUNDS = 5
_NO_CHUNKS_TEMPLATE = "未在已上传文档中找到相关依据。"
_SCORE_THRESHOLD = 0.3
LlmFactory = Any  # Callable[[LlmConfig], BaseLlmClient]


class RagQaService:
    """doc_qa 全流程编排。"""

    def __init__(
        self,
        *,
        rag_service: RagService | None = None,
        llm_factory: LlmFactory | None = None,
    ) -> None:
        self._rag_svc = rag_service or RagService()
        self._llm_factory = llm_factory

    async def load_history(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        limit: int = _HISTORY_ROUNDS,
    ) -> list[dict[str, str]]:
        """加载上 N 轮 doc_qa 消息，返回 [{role, content}]，不含 citations。

        按 created_time 倒序取最近 N 条；过滤 channel='doc_qa'。
        返回新列表，不修改入参。
        """
        stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.channel == "doc_qa",
            )
            .order_by(SessionMessage.created_time.desc())
            .limit(limit * 2)  # 取两倍以凑齐 N 轮 user+assistant
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())
        # 倒序恢复时间正序；只保留 role + content，丢弃 citations
        out: list[dict[str, str]] = []
        for row in reversed(rows):
            out.append({"role": row.role, "content": row.content})
            if len(out) >= limit * 2:
                break
        return out

    async def _persist(
        self,
        session: AsyncSession,
        dto: DocQaRequest,
        answer: str,
        citations: list[dict[str, Any]],
        *,
        actor: CurrentUser,
    ) -> None:
        """落库 user 行 + assistant 行（doc_qa channel）。"""
        user_msg = SessionMessage(
            session_id=dto.session_id,
            role="user",
            content=dto.question,
            channel="doc_qa",
            user_id=actor.userId,
        )
        session.add(user_msg)

        asst_msg = SessionMessage(
            session_id=dto.session_id,
            role="assistant",
            content=answer,
            question=dto.question,
            citations=citations or None,
            channel="doc_qa",
            user_id=actor.userId,
        )
        session.add(asst_msg)
        await session.flush()
        await session.commit()

    async def answer_stream(
        self,
        session: AsyncSession,
        dto: DocQaRequest,
        *,
        actor: CurrentUser,
        configs: list[LlmConfig],
    ) -> AsyncIterator[StreamEvent]:
        """一轮 doc_qa SSE 事件序列（流式生成器）。

        无相关 chunks → meta + citations=[] + token(template) + done，零 LLM 调用；
        有 chunks → meta + citations + token×N + done，期间调 LLM。
        """
        # 1. 加载历史
        history = await self.load_history(session, dto.session_id)
        history_block = format_history_block(history)

        # 2. 检索
        chunks = await self._rag_svc.searchDocuments(
            dto.question,
            top_k=dto.top_k,
            security_level=dto.security_level,
            session=session,
        )

        # 3. 加 id 给 LLM 引用
        citations = [{"id": idx + 1, **c} for idx, c in enumerate(chunks)]

        # 4. 事件先行：meta + citations
        yield StreamEvent(EVENT_QA_META, {"intent": "doc_qa"})
        yield StreamEvent(EVENT_QA_CITATIONS, {"citations": citations})

        # 5. 短路：top1 score 过低 → 固定模板
        if not chunks or chunks[0].get("score", 0.0) < _SCORE_THRESHOLD:
            yield StreamEvent(EVENT_TOKEN, {"content": _NO_CHUNKS_TEMPLATE})
            yield StreamEvent(EVENT_QA_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})
            await self._persist(session, dto, _NO_CHUNKS_TEMPLATE, [], actor=actor)
            return

        # 6. 选模型（dto.model_id 优先）
        if dto.model_id is not None:
            selected = next((c for c in configs if c.id == dto.model_id), None)
            # 既不存在（id 不匹配）也已停用（is_active=False）都视为不可用，
            # 复用既有 MSG_MODEL_CONFIG_UNAVAILABLE 消息（声明「不存在或已禁用」）。
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
        #    旧实现 factory 缺省 None，prod 直跑必 RuntimeError，feat-wiki-chat 同步修正）
        factory = self._llm_factory
        if factory is None:
            from app.infrastructure.llm.factory import createClient
            factory = createClient
        client = factory(selected)
        if client is None:
            from app.domain.exceptions import LLMUnavailableError
            raise LLMUnavailableError("未配置可用的 LLM，无法进行文档问答")

        chunks_json = format_chunks_json(chunks)
        messages: list[LlmMessage] = [
            LlmMessage(role="system", content=DOC_QA_SYSTEM_PROMPT.format(chunks_json=chunks_json)),
            LlmMessage(
                role="user",
                content=DOC_QA_USER_TEMPLATE.format(
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

        # 9. 计算 cost
        cost = (
            Decimal(total_pt) * Decimal(str(selected.cost_per_1k_input))
            + Decimal(total_ct) * Decimal(str(selected.cost_per_1k_output))
        ) / Decimal(1000)

        # 10. 落库（user + assistant）
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
