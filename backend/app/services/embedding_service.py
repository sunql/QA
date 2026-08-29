"""Embedding 服务：查询向量生成、历史查询存储、相似问法检索。

查询存储为 fire-and-forget：可预期的失败（LLM / Milvus / 网络）仅记录日志，不阻断问答主流程。
相似检索为显式动作：embedding / milvus 失败时向上抛领域异常（API 层转 4xx）。

性能说明：pymilvus 为同步 SDK，网络 I/O 通过 asyncio.to_thread 移出事件循环，
避免阻塞 FastAPI 异步请求处理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import app.infrastructure.milvus_client as milvus
from pymilvus.exceptions import MilvusException

from app.domain.exceptions import DomainError, MilvusError
from app.domain.schemas import SimilarQuery
from app.services.messages_zh import MSG_VECTOR_SEARCH_FAILED
from app.infrastructure.llm.embedding_client import EmbeddingClient

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 5


def _distanceToSimilarity(distance: float) -> float:
    """Milvus L2 距离 → [0,1] 相似度（距离越小越相似；负数按 0 处理防除零）。"""
    return round(1.0 / (1.0 + max(float(distance), 0.0)), 4)


class EmbeddingService:
    """查询向量生成与相似检索门面。"""

    def __init__(self, *, client: EmbeddingClient | None = None) -> None:
        self._client = client  # 测试注入；否则延迟到首次使用

    async def _ensureClient(self) -> EmbeddingClient:
        """解析 embedding 客户端：注入的优先，否则走 provider 注册表（resolver 持有缓存）。

        resolver 返回的客户端归 resolver 所有（失效时由其关闭），故不缓存回
        self._client——close() 只关闭测试注入的客户端。
        """
        if self._client is not None:
            return self._client
        from app.infrastructure.llm.embedding_provider_factory import (
            getActiveEmbeddingClient,
        )

        return await getActiveEmbeddingClient()

    async def close(self) -> None:
        """释放注入的 embedding 客户端连接（幂等；未使用时 no-op）。

        resolver 解析的客户端由 embedding_provider_factory 失效时关闭。
        """
        if self._client is None:
            return
        client = self._client
        self._client = None
        await client.close()

    async def generateEmbedding(self, text: str) -> list[float]:
        """为单条文本生成向量（返回新列表，不改动客户端内部数据）。"""
        vectors = await (await self._ensureClient()).embed([text])
        return list(vectors[0])

    async def storeQueryEmbedding(
        self,
        *,
        sessionId: str,
        question: str,
        sql: str,
        datasourceId: int | None = None,
    ) -> None:
        """存储一条历史查询向量。fire-and-forget：可预期失败仅记录日志，不向上抛。

        捕获所有 DomainError（含 resolver 维度守卫 ConfigError）：embedding 配置错误
        只影响向量存储，不应以未处理异常逃逸到后台任务日志。
        """
        try:
            embedding = await (await self._ensureClient()).embed([question])
            await asyncio.to_thread(
                milvus.insertQueryEmbedding,
                sessionId=sessionId,
                question=question,
                sql=sql,
                embedding=embedding[0],
                datasourceId=datasourceId,
            )
        except (DomainError, MilvusException, OSError) as exc:
            logger.warning("历史查询向量存储失败 session=%s: %s", sessionId, exc, exc_info=True)

    async def searchSimilarQueries(
        self,
        question: str,
        *,
        topK: int = _DEFAULT_TOP_K,
        datasourceId: int | None = None,
    ) -> list[SimilarQuery]:
        """检索与问题语义最相似的历史查询，按相似度从高到低返回。"""
        embedding = await (await self._ensureClient()).embed([question])
        try:
            hits = await asyncio.to_thread(
                milvus.searchQueryEmbedding, embedding[0], topK=topK, datasourceId=datasourceId
            )
        except (MilvusException, OSError) as exc:
            raise MilvusError(MSG_VECTOR_SEARCH_FAILED, detail=str(exc)) from exc
        return [
            SimilarQuery(
                question=h["question"],
                sql=h.get("sql"),
                similarity=_distanceToSimilarity(h["distance"]),
            )
            for h in hits
        ]
