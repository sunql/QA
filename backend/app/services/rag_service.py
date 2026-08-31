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

from app.dependencies import getDb
from app.domain.enums import DocumentSecurityLevel
from app.domain.schemas import DocumentCreate, DocumentUpdate
from app.domain.exceptions import ConflictError
from app.services.document_parser import DocumentParserError, parse_document
from app.services.chunk_splitter import Chunk, split_by_paragraphs
from app.services.document_service import DocumentService
from app.infrastructure.milvus_client import insertDocumentChunks, searchDocumentChunks

logger = logging.getLogger(__name__)

# 向量化服务延迟导入（避免无 Milvus 时无法 import）
_embedding_service = None


def _getEmbeddingService():
    global _embedding_service
    if _embedding_service is None:
        from app.services.embedding_service import getEmbeddingService
        _embedding_service = getEmbeddingService()
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
    ) -> dict:
        """文档入库全流程。

        1. 解析文本
        2. 切分 chunk
        3. 生成 embedding
        4. 写入 Milvus
        5. 写入 document_catalog（若 document_id 未存在）

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
            {"document_id": ..., "chunks": 数量, "status": "ingested"}

        Raises:
            RagError: 处理链中任何一步失败
        """
        # 1. 解析
        try:
            text = await parse_document(content, mime_type, filename)
        except DocumentParserError as e:
            raise RagError(f"文档解析失败: {e}") from e

        if not text.strip():
            raise RagError("文档内容为空，无法入库")

        # 2. 分块
        chunks: list[Chunk] = split_by_paragraphs(text)
        if not chunks:
            raise RagError("分块结果为空")

        # 3. 生成 embedding
        embedding_service = _getEmbeddingService()
        texts_for_embed = [c.text for c in chunks]
        try:
            embeddings = await embedding_service.embed_texts(texts_for_embed)
        except Exception as e:
            raise RagError(f"Embedding 生成失败: {e}") from e

        if len(embeddings) != len(chunks):
            raise RagError(
                f"Embedding 数量不匹配: {len(embeddings)} vs {len(chunks)} chunks"
            )

        # 4. 写入 Milvus
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
                "embedding": emb,
            })

        try:
            insertDocumentChunks(records)
        except Exception as e:
            raise RagError(f"Milvus 写入失败: {e}") from e

        # 5. 写入 document_catalog（幂等：若已存在则更新，否则创建）
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
                    storage_url=None,
                    content_hash=None,
                ),
            )
        else:
            # 更新 storage_url（无实际文件存储 URL，这里记录处理状态）
            try:
                await self._doc_svc.updateDocument(
                    session,
                    existing_doc.id,
                    DocumentUpdate(storage_url=f"milvus://{len(chunks)}_chunks"),
                )
            except Exception:
                pass  # 更新失败不影响主流程

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
        }

    async def searchDocuments(
        self,
        query_text: str,
        *,
        security_level: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """语义检索文档 chunks。

        Args:
            query_text: 自然语言查询
            security_level: 可选，按安全等级过滤
            top_k: 返回数量

        Returns:
            匹配的 chunk 列表，含 document_id, chunk_text, distance
        """
        embedding_service = _getEmbeddingService()
        try:
            query_emb = await embedding_service.embed_texts([query_text])
        except Exception as e:
            raise RagError(f"Query embedding 失败: {e}") from e

        hits = searchDocumentChunks(
            query_emb[0],
            securityLevel=security_level,
            topK=top_k,
        )
        return [
            {
                "document_id": h["document_id"],
                "chunk_id": h["chunk_id"],
                "chunk_text": h["chunk_text"],
                "chunk_sequence": h["chunk_sequence"],
                "distance": h["distance"],
            }
            for h in hits
        ]
