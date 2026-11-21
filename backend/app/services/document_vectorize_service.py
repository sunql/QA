"""文档向量化（把已入库文档的原文灌进 ``document_embeddings``）。

背景（需求 3）：wiki 导入的文件会进 ``document_catalog``、但**不生成向量**
⇒ 采购知识检索 / Copilot 搜不到它。本服务把「已存源文件 → 原文 chunk →
向量」这一步补上，让**同一份资料的两份投影**都可用：

```
同一份源文件（MinIO 一份字节）
   ├─→ wiki_page_embeddings   （wiki 侧 LLM 抽取的结构化知识）
   └─→ document_embeddings     （本服务：原文 chunk 切片）
```

**不重新上传、不重新解析用户文件**：从 MinIO 读回**已存的字节**，
重新走切块 → embedding → 写向量。这样 wiki 侧不必为了向量化再传一遍。

三条纪律：
1. **幂等**：先按 ``document_id`` 删旧向量再插（``deleteDocumentChunks``），
   否则重复调用会产生重复 chunk（``document_embeddings`` 无唯一约束）。
2. **ACL**：向量化会消耗 embedding 额度（**花钱**）⇒ 只有文档 owner 部门
   成员与 admin 能触发；普通登录用户点了也不能白烧钱。
3. **失败不半途**：解析/embedding 失败 ⇒ 不删旧向量（避免把已有可检索的
   文档搞成「检索不到」）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import PermissionDeniedError, ValidationError
from app.domain.models import DocumentCatalog
from app.services.messages_zh import (
    MSG_DOCUMENT_VECTORIZE_FORBIDDEN,
    MSG_DOCUMENT_VECTORIZE_NO_SOURCE,
)

logger = logging.getLogger(__name__)

#: 向量化的最小长度门限（比这个短的文档切不出有意义的 chunk）。
_MIN_CHUNK_COUNT = 1


@dataclass(frozen=True)
class VectorizeResult:
    """向量化结果。"""

    documentId: str
    chunks: int
    #: True = 之前已有向量，本次是**重建**（不是首次）
    replaced: bool


def assertCanVectorize(
    catalog: DocumentCatalog,
    actor: object,
    *,
    isAdmin: bool = False,
) -> None:
    """写 ACL：只有 owner 部门成员或 admin 能向量化。

    向量化会**花钱**（embedding 调用）且会写共享向量表 ⇒ 不能是任意登录
    用户可触发的动作。
    """
    if isAdmin:
        return
    actorDepartments = set(getattr(actor, "departments", None) or ())
    owner = catalog.owner
    if owner and owner in actorDepartments:
        return
    raise PermissionDeniedError(MSG_DOCUMENT_VECTORIZE_FORBIDDEN)


async def vectorizeDocument(
    session: AsyncSession,
    catalog: DocumentCatalog,
    *,
    actor: object,
    isAdmin: bool = False,
) -> VectorizeResult:
    """把已入库文档的原文灌进 ``document_embeddings``（幂等）。

    流程：读回源文件 → 解析 → 切块 → embedding → **删旧向量** → 写新向量
    → 记 ``vectorized_at``。
    """
    from app.infrastructure.milvus_client import (
        deleteDocumentChunks,
        insertDocumentChunks,
        queryDocumentChunks,
    )
    from app.services.chunk_splitter import split_by_paragraphs
    from app.services.document_parser import parse_document
    from app.services.document_source_service import (
        filenameFromStorageUrl,
        readSourceBytes,
    )

    assertCanVectorize(catalog, actor, isAdmin=isAdmin)

    content = await readSourceBytes(catalog.storage_url)
    if content is None:
        raise ValidationError(MSG_DOCUMENT_VECTORIZE_NO_SOURCE)

    filename = filenameFromStorageUrl(
        catalog.storage_url or "", catalog.document_name
    )
    mimeType = catalog.mime_type or "application/octet-stream"

    # 1. 解析 + 切块（失败 ⇒ 不动已有向量）
    try:
        blocks = await parse_document(content, mimeType, filename)
    except Exception as exc:  # noqa: BLE001 —— 解析失败要报清楚，但不半途删除
        logger.warning(
            "文档向量化：解析失败 document_id=%s filename=%s",
            catalog.document_id,
            filename,
            exc_info=True,
        )
        raise ValidationError(f"文档解析失败，无法向量化：{filename}") from exc
    chunks = split_by_paragraphs(blocks)
    if len(chunks) < _MIN_CHUNK_COUNT:
        raise ValidationError(f"文档内容为空，无法向量化：{filename}")

    # 2. embedding
    from app.services.rag_service import _getEmbeddingService

    embeddingService = _getEmbeddingService()
    embeddings = [
        await embeddingService.generateEmbedding(chunk.text) for chunk in chunks
    ]
    if len(embeddings) != len(chunks):
        raise ValidationError("向量化失败：Embedding 数量与分块数不一致")

    # 3. 幂等：先删旧向量再写（**放在最后**⇒ 中途失败不会把文档变成搜不到）
    existing = queryDocumentChunks(catalog.document_id)
    replaced = len(existing) > 0
    if replaced:
        deleteDocumentChunks(catalog.document_id)

    effectiveDate = (
        catalog.effective_date.isoformat() if catalog.effective_date else ""
    )
    securityLevel = str(
        getattr(catalog.security_level, "value", catalog.security_level)
    )
    records = [
        {
            "document_id": catalog.document_id,
            "chunk_id": chunk.chunk_id,
            "chunk_text": chunk.text,
            "chunk_sequence": chunk.sequence,
            "effective_date": effectiveDate,
            "security_level": securityLevel,
            "page_number": chunk.metadata.get("page_number"),
            "section_name": chunk.metadata.get("section_name"),
            "paragraph_no": chunk.metadata.get("paragraph_no"),
            "embedding": embedding,
        }
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]
    insertDocumentChunks(records)

    # 4. 记向量化时间（目录页据此显示状态）
    catalog.vectorized_at = datetime.now(UTC)
    await session.commit()

    logger.info(
        "文档向量化完成: document_id=%s chunks=%d replaced=%s",
        catalog.document_id,
        len(chunks),
        replaced,
    )
    return VectorizeResult(
        documentId=catalog.document_id,
        chunks=len(chunks),
        replaced=replaced,
    )
