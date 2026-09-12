"""Phase 5.2 RAG 服务（文档 → 分块 → 向量化 → Milvus）。

编排：解析文档 → 切分 chunk → 生成 embedding → 写入 Milvus document collection。
业务元数据写入 document_catalog 表（由 DocumentService 负责）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getDb
from app.domain.enums import DocumentSecurityLevel
from app.domain.schemas import DocumentCreate, DocumentUpdate
from app.domain.exceptions import ConflictError
from app.services.document_parser import DocumentParserError, parse_document
from app.services.chunk_splitter import Chunk, split_by_paragraphs
from app.services.document_service import DocumentService
from app.infrastructure.milvus_client import insertDocumentChunks, searchDocumentChunks
from app.infrastructure.object_storage import (
    ObjectStorageError,
    buildSourceObjectName,
    hashContent,
    putSourceObject,
)

logger = logging.getLogger(__name__)

# 向量化服务延迟导入（避免无 Milvus 时无法 import）
_embedding_service = None


def _getEmbeddingService():
    """返回 EmbeddingService 单例（懒初始化）。"""
    global _embedding_service
    if _embedding_service is None:
        from app.services.embedding_service import EmbeddingService
        _embedding_service = EmbeddingService()
    return _embedding_service


class RagError(Exception):
    """RAG 处理失败。"""
    pass


class RagService:
    """RAG 全链路服务。"""

    def __init__(self) -> None:
        self._doc_svc = DocumentService()

    async def ingestDocument(
        self,
        session: AsyncSession,
        content: bytes,
        filename: str,
        mime_type: str,
        *,
        document_id: str | None = None,
        document_name: str | None = None,
        document_type: str = "OTHER",
        version: str = "v1.0",
        owner: str = "",
        effective_date: str | None = None,
        security_level: str = "L1",
        actor: CurrentUser,
    ) -> dict:
        """文档入库全流程。

        1. 解析文本（返回带定位符的文本块）
        2. 源文件留存（对象存储，内容寻址）
        3. 切分 chunk
        4. 生成 embedding
        5. 写入 Milvus
        6. 写入 document_catalog（若 document_id 未存在）

        Args:
            session: DB session
            content: 文件字节
            filename: 原始文件名
            mime_type: MIME 类型
            document_id: 业务文档编号（不传则自动生成）
            document_name: 文档名
            document_type: DocumentType enum 值
            version: 版本
            owner: 责任部门/人
            effective_date: 生效日期 YYYY-MM-DD
            security_level: L1/L2/L3

        Returns:
            {"document_id": ..., "chunks": 数量, "status": "ingested",
             "storage_url": ..., "content_hash": ...}

        Raises:
            RagError: 处理链中任何一步失败
        """
        # 1. 解析（返回带定位符的文本块）
        try:
            blocks = await parse_document(content, mime_type, filename)
        except DocumentParserError as e:
            raise RagError(f"文档解析失败: {e}") from e

        if not blocks:
            raise RagError("文档内容为空，无法入库")

        # 2. 源文件留存（内容寻址；失败必须显式，不得静默跳过）
        contentHash = hashContent(content)
        objectName = buildSourceObjectName(contentHash, filename)
        try:
            storageUrl = putSourceObject(objectName, content, mime_type)
        except ObjectStorageError as e:
            raise RagError(f"源文件存储失败: {e}") from e

        # 3. 分块
        chunks: list[Chunk] = split_by_paragraphs(blocks)
        if not chunks:
            raise RagError("分块结果为空")

        # 4. 生成 embedding（逐条调用 EmbeddingService.generateEmbedding）
        embedding_service = _getEmbeddingService()
        try:
            embeddings = [await embedding_service.generateEmbedding(c.text) for c in chunks]
        except Exception as e:
            raise RagError(f"Embedding 生成失败: {e}") from e

        if len(embeddings) != len(chunks):
            raise RagError(
                f"Embedding 数量不匹配: {len(embeddings)} vs {len(chunks)} chunks"
            )

        # 5. 写入 Milvus
        doc_id = document_id or f"DOC-{uuid.uuid4().hex[:12].upper()}"
        effective_str = effective_date or ""
        records: list[dict] = []
        for chunk, emb in zip(chunks, embeddings):
            records.append({
                "document_id": doc_id,
                "chunk_id": chunk.chunk_id,
                "chunk_text": chunk.text,
                "chunk_sequence": chunk.sequence,
                "effective_date": effective_str,
                "security_level": security_level,
                "page_number": chunk.metadata.get("page_number"),
                "section_name": chunk.metadata.get("section_name"),
                "paragraph_no": chunk.metadata.get("paragraph_no"),
                "embedding": emb,
            })

        try:
            insertDocumentChunks(records)
        except Exception as e:
            raise RagError(f"Milvus 写入失败: {e}") from e

        # 6. 写入 document_catalog（幂等：若已存在则更新，否则创建）
        if not document_name:
            document_name = filename

        try:
            existing = await self._doc_svc.getDocument(session, 0)
        except Exception:
            existing = None

        # 检查 document_id 是否已存在（通过 listDocuments 过滤）
        existing_docs = await self._doc_svc.listDocuments(session, document_type=document_type)
        existing_doc = next(
            (d for d in existing_docs if d.document_id == doc_id), None
        )

        if existing_doc is None:
            await self._doc_svc.createDocument(
                session,
                DocumentCreate(
                    document_id=doc_id,
                    document_name=document_name,
                    document_type=document_type,
                    version=version,
                    owner=owner,
                    effective_date=effective_date,
                    security_level=security_level,
                    storage_url=storageUrl,
                    content_hash=contentHash,
                ),
                actor=actor,
            )
        else:
            # 已存在：刷新真实存储信息。
            # 原实现写的是 f"milvus://{len(chunks)}_chunks" 这种假 URL，而且
            # 漏传了 actor —— updateDocument(session, id, dto, actor) 四个
            # 参数，只传三个必然 TypeError，又被下面的 except Exception: pass
            # 吞掉，所以这个分支在生产里从未真正生效过。两处一起修。
            await self._doc_svc.updateDocument(
                session,
                existing_doc.id,
                DocumentUpdate(storage_url=storageUrl, content_hash=contentHash),
                actor=actor,
            )

        logger.info(
            "RAG ingest done: doc=%s, filename=%s, chunks=%d",
            doc_id,
            filename,
            len(chunks),
        )
        return {
            "document_id": doc_id,
            "filename": filename,
            "chunks": len(chunks),
            "status": "ingested",
            "storage_url": storageUrl,
            "content_hash": contentHash,
        }

    async def searchDocuments(
        self,
        query_text: str,
        *,
        security_level: str | None = None,
        top_k: int = 5,
        session: AsyncSession | None = None,
    ) -> list[dict]:
        """语义检索文档 chunks。

        Args:
            query_text: 自然语言查询
            security_level: 可选，按安全等级过滤
            top_k: 返回数量
            session: DB session（可选，传入则按 document_id JOIN document_catalog
                     回填每条 hit 的 document_name；不传则仅返回 Milvus 字段）

        Returns:
            匹配的 chunk 列表，含 document_id, chunk_text, chunk_sequence,
            distance, score；session 存在时还含 document_name（document_id 回退）。
        """
        embedding_service = _getEmbeddingService()
        try:
            query_emb = await embedding_service.generateEmbedding(query_text)
        except Exception as e:
            raise RagError(f"Query embedding 失败: {e}") from e

        hits = searchDocumentChunks(
            query_emb,
            securityLevel=security_level,
            topK=top_k,
        )
        if not hits:
            return []

        # 用 Milvus 命中 document_id 反查 document_catalog → document_name，
        # 前端 DocumentsPage 卡片标题需要；缺则降级回 document_id 本身。
        name_by_id: dict[str, str] = {}
        if session is not None:
            unique_ids = list({h["document_id"] for h in hits if h.get("document_id")})
            try:
                rows = await self._doc_svc.listDocuments(
                    session, document_ids=unique_ids,
                )
                name_by_id = {r.document_id: r.document_name for r in rows}
            except Exception:
                # 回填失败不应阻塞响应：降级为只有 Milvus 字段
                logger.warning(
                    "searchDocuments: 回填 document_name 失败，仅返回 Milvus 字段",
                    exc_info=True,
                )

        return [
            {
                "document_id": h["document_id"],
                "chunk_id": h["chunk_id"],
                "chunk_text": h["chunk_text"],
                "chunk_sequence": h["chunk_sequence"],
                "distance": h["distance"],
                # Milvus 用 L2 距离（越小越相似）；前端展示需要"相似度"。
                # 公式 score = 1 / (1 + distance) 把 [0, ∞) 映射到 (0, 1]：
                #   distance=0 → 1.0（完全相同）；distance=1 → 0.5；distance→∞ → 0。
                # DocumentsPage.tsx 直接读 item.score 渲染百分比，必须存在。
                "score": 1.0 / (1.0 + h["distance"]),
                "document_name": name_by_id.get(
                    h["document_id"], h["document_id"],
                ),
            }
            for h in hits
        ]
