"""文档问答 RAG 服务。

编排：加载上 5 轮历史 → Milvus 检索 → LLM 流式合成 → 落库。
无相关 chunks（top1 score < 0.3）走固定模板，零 LLM 调用。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
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
            return

        # 6. LLM 流式（占位，Task 4 实现）
        # 此处保留供 Task 4 填充完整 LLM 流式逻辑
        raise NotImplementedError("Task 4 填充：LLM 流式 + 落库 + done 事件")
