"""Wiki 知识条目向量化服务（feat-wiki-semantic-search）。

编排：Markdown 正文切分 → embedding 生成 → 写入 Milvus ``wiki_page_embeddings``
集合（upsert = 先按 page_id 删旧再插新）。语义检索侧：查询向量 → Milvus 相似
检索 → 回查 PG 用**最新** title/status/dimension 覆盖 Milvus 快照（PG 是 SSOT，
Milvus 副本在条目更新瞬间可能陈旧）。

失败语义：本服务抛 ``WikiVectorError``；写路径挂钩方（wiki_page_service /
wiki_import_service）负责 best-effort 包裹——向量同步失败不阻断条目 CRUD，
靠 :meth:`backfill` 对账自愈。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import WikiPage
from app.infrastructure import milvus_client as milvus
from app.services.chunk_splitter import TextBlock, split_by_paragraphs

logger = logging.getLogger(__name__)

# 与 chunk_splitter 默认一致；wiki 正文较短，沿用文档口径
_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 50

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")


class WikiVectorError(Exception):
    """wiki 向量化 / 语义检索链路失败（embedding、Milvus、连接）。"""


# embedding 计量的机制名（LEARNING_MECHANISMS 白名单已加 RETRIEVE）
_MECHANISM_RETRIEVE = "RETRIEVE"


async def _meterEmbedding(
    *,
    promptTokens: int,
    modelName: str | None,
    purpose: str,
) -> None:
    """把一次 embedding 调用的 token 用量写入 wiki_token_usage。

    核心约束 #3「每次 LLM 调用必须记录 Token 消耗」同样覆盖 embedding。
    用**独立的短命 session**（而非调用方的 session）：agent 工具 / 检索请求
    的主事务由别的层收口，计量行不该被它们的回滚或提交时机绑架。
    best-effort：计量失败只告警，不阻断检索/同步本身。
    cost 恒为 0：embedding_provider 表无单价字段（本地模型免费），落 0 而非编造。
    """
    if promptTokens <= 0:
        return
    try:
        from decimal import Decimal

        from app.infrastructure.database import getSessionFactory
        from app.services.wiki_token_usage_service import WikiTokenUsageService

        async with getSessionFactory()() as session:
            await WikiTokenUsageService().record(
                session,
                mechanism=_MECHANISM_RETRIEVE,
                modelConfigId=None,
                modelName=modelName,
                promptTokens=promptTokens,
                completionTokens=0,
                cost=Decimal("0"),
                purpose=purpose,
            )
            await session.commit()
    except Exception:
        logger.warning("wiki embedding 计量落库失败（best-effort）", exc_info=True)


def _getEmbeddingService():
    """返回 EmbeddingService 单例（懒初始化，避免无 Milvus 环境无法 import）。"""
    from app.services.embedding_service import EmbeddingService

    return EmbeddingService()


def splitMarkdownBlocks(content: str) -> list[TextBlock]:
    """把 Markdown 正文按空行切成 TextBlock，段落跟随最近的标题（section_name）。"""
    blocks: list[TextBlock] = []
    currentHeading = ""
    paragraphNo = 0
    for raw in content.split("\n\n"):
        text = raw.strip()
        if not text:
            continue
        paragraphNo += 1
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        headingLines = [m.group(1).strip() for ln in lines if (m := _HEADING_RE.match(ln))]
        if headingLines:
            # 纯标题段：更新当前标题，不产出正文块
            currentHeading = headingLines[-1]
            if len(lines) == len(headingLines):
                continue
        body = "\n".join(ln for ln in lines if not _HEADING_RE.match(ln))
        if not body:
            continue
        blocks.append(TextBlock(
            text=body,
            page_number=None,
            section_name=currentHeading,
            paragraph_no=paragraphNo,
        ))
    return blocks


class WikiVectorService:
    """wiki 条目向量 upsert / 删除 / 语义检索 / 全量回填。"""

    def __init__(self, *, embeddingService: Any | None = None) -> None:
        self._embSvc = embeddingService

    def _ensureEmbSvc(self):
        if self._embSvc is None:
            self._embSvc = _getEmbeddingService()
        return self._embSvc

    async def _embedWithUsage(
        self, texts: list[str]
    ) -> tuple[list[list[float]], int, str | None]:
        """批量生成向量 + ``(向量, prompt_tokens, 模型名)``（计量用）。

        用**类级属性**探测而非 getattr：测试注入的 MagicMock 会对任意属性名
        自动生成 mock，getattr 永远非 None；而类级 hasattr 对 MagicMock 为 False、
        对真实门面/客户端为 True。都没有的替身回退逐条 generateEmbedding，
        token 记 0（不产计量行）。
        """
        svc = self._ensureEmbSvc()
        if hasattr(type(svc), "embedWithUsage"):
            return await svc.embedWithUsage(texts)
        vectors = [await svc.generateEmbedding(t) for t in texts]
        return vectors, 0, None

    # ------------------------------------------------------------------
    # upsert / delete
    # ------------------------------------------------------------------

    async def syncPage(self, page: WikiPage) -> int:
        """同步单条知识条目的向量（upsert）。返回写入的 chunk 数。

        先删旧向量再插新（Milvus 无按业务键 update）；正文清空时只删不插。
        """
        chunks = split_by_paragraphs(
            splitMarkdownBlocks(page.content or ""),
            chunk_size=_CHUNK_SIZE,
            overlap=_CHUNK_OVERLAP,
        )
        try:
            await asyncio.to_thread(milvus.deleteWikiPageChunks, page.page_id)
            if not chunks:
                return 0
            texts = [c.text for c in chunks]
            embeddings, promptTokens, embModel = await self._embedWithUsage(texts)
            records = [
                {
                    "page_id": page.page_id,
                    "chunk_id": f"{page.page_id}:{c.chunk_id}",
                    "chunk_text": c.text,
                    "chunk_sequence": c.sequence,
                    "title": page.title,
                    "dimension": page.dimension,
                    "status": page.status,
                    "embedding": emb,
                }
                for c, emb in zip(chunks, embeddings)
            ]
            await asyncio.to_thread(milvus.insertWikiPageChunks, records)
            await _meterEmbedding(
                promptTokens=promptTokens,
                modelName=embModel,
                purpose="wiki_vector_sync",
            )
            return len(records)
        except WikiVectorError:
            raise
        except Exception as e:
            raise WikiVectorError(f"wiki 向量同步失败 page_id={page.page_id}: {e}") from e

    async def deletePageVectors(self, pageId: str) -> None:
        """删除条目的全部向量（删除条目时调用）。"""
        try:
            await asyncio.to_thread(milvus.deleteWikiPageChunks, pageId)
        except Exception as e:
            raise WikiVectorError(f"wiki 向量删除失败 page_id={pageId}: {e}") from e

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------

    async def searchSemantic(
        self,
        session: AsyncSession,
        query: str,
        *,
        dimension: str | None = None,
        topK: int = 10,
    ) -> list[dict[str, Any]]:
        """语义检索知识条目。

        Returns:
            按 score 降序的 hit 列表：pageId / title / status / dimension /
            chunkText / chunkSequence / distance / score。
            title 等以 PG 最新值为准（Milvus 快照仅降级兜底）。
        """
        try:
            queryEmb, promptTokens, embModel = await self._embedWithUsage([query])
            hits = await asyncio.to_thread(
                milvus.searchWikiPageChunks,
                queryEmb[0],
                dimension=dimension,
                topK=topK,
            )
        except WikiVectorError:
            raise
        except Exception as e:
            raise WikiVectorError(f"wiki 语义检索失败: {e}") from e

        await _meterEmbedding(
            promptTokens=promptTokens,
            modelName=embModel,
            purpose="wiki_semantic_search",
        )

        if not hits:
            return []

        # 回查 PG 覆盖快照字段；缺失（已删/回查失败）降级 Milvus 字段
        metaByPageId: dict[str, WikiPage] = {}
        pageIds = list({h["page_id"] for h in hits if h.get("page_id")})
        if pageIds:
            try:
                rows = (
                    await session.execute(
                        select(WikiPage).where(WikiPage.page_id.in_(pageIds))
                    )
                ).scalars().all()
                metaByPageId = {r.page_id: r for r in rows}
            except Exception:
                logger.warning("searchSemantic: 回查 wiki_page 失败，降级 Milvus 快照", exc_info=True)

        results: list[dict[str, Any]] = []
        for h in hits:
            row = metaByPageId.get(h["page_id"])
            results.append({
                "pageId": h["page_id"],
                "title": row.title if row is not None else (h.get("title") or h["page_id"]),
                "status": row.status if row is not None else (h.get("status") or ""),
                "dimension": row.dimension if row is not None else (h.get("dimension") or None),
                "chunkText": h.get("chunk_text") or "",
                "chunkSequence": h.get("chunk_sequence"),
                "distance": h["distance"],
                # 与 rag_service.searchDocuments 同口径：L2 距离越小越相似
                "score": 1.0 / (1.0 + max(float(h["distance"]), 0.0)),
            })
        return results

    # ------------------------------------------------------------------
    # 全量回填（对账自愈入口）
    # ------------------------------------------------------------------

    async def backfill(self, session: AsyncSession) -> dict[str, int]:
        """遍历全部知识条目逐条 upsert 向量；幂等，可重复跑对账。"""
        rows = (
            (await session.execute(select(WikiPage))).scalars().all()
        )
        totalChunks = 0
        for page in rows:
            totalChunks += await self.syncPage(page)
        logger.info("wiki vector backfill: %d pages, %d chunks", len(rows), totalChunks)
        return {"pages": len(rows), "chunks": totalChunks}

    async def syncPagesBestEffort(
        self, session: AsyncSession, pageIds: list[str]
    ) -> None:
        """按 page_id 批量同步向量，best-effort（失败仅告警）。

        导入任务收尾用：导入走直插 ORM 绕过了 WikiPageService 的挂钩，
        在任务成功收尾时统一补同步。
        """
        if not pageIds:
            return
        rows = (
            (
                await session.execute(
                    select(WikiPage).where(WikiPage.page_id.in_(pageIds))
                )
            )
            .scalars()
            .all()
        )
        for page in rows:
            try:
                await self.syncPage(page)
            except Exception:
                logger.warning(
                    "导入收尾向量同步失败（best-effort）page_id=%s",
                    page.page_id,
                    exc_info=True,
                )
