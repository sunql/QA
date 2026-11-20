"""M2-3 采购文档检索（设计稿 §8.3 `POST /procurement/knowledge/documents/search`）。

**复用既有检索与密级闸**：`RagService.searchDocuments`（Milvus chunk 检索 +
Task 4.3 密级过滤经 user 透传 + catalog 回填）；本模块只做**编排放大**：

1. 取回 chunks 后按 catalog 全行核验：**无 catalog 行 ⇒ 丢弃**（密级/有效期
   无法核验 ⇒ fail-closed，对齐「检索不返回未完成版本」）；非 ACTIVE 丢弃；
2. 过滤：documentTypes（catalog 类型）/ entityKeys（`TYPE:KEY` 形式，命中
   `document_entity_relation` 任一即保留）/ effectiveAt（生效期未知=保留）；
3. **M2-1 契约回填**：`security.level`（catalog 密级）/ `sourceRef.documentId` /
   `validAt`（catalog 生效日）/ `confidence`（1 - distance）；
4. **provenance**：`source=LIVE`——文档不在本体发布面，但 `catalog.version`
   持久可溯（与治理表「原地 UPDATE 无版本可引」不同，引用可信）。

已知限制（登记）：Milvus 侧不支持按文档类型/实体过滤 ⇒ 先按 topK 取回后
内存过滤，过滤后条数可能少于 topK（先取后滤的固有形态）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentStatus
from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_SOURCE_LIVE,
    KnowledgeEvidenceRead,
    KnowledgeEvidenceSecurity,
    KnowledgeEvidenceSourceRef,
)
from app.domain.models import DocumentCatalog, DocumentEntityRelation
from app.services.rag_service import RagService

logger = logging.getLogger(__name__)

#: chunk 文本进 snippet 的上限（对齐 RAG 路的 MAX_SNIPPET_LENGTH 口径）。
MAX_SNIPPET_LENGTH = 300


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SNIPPET_LENGTH else text[:MAX_SNIPPET_LENGTH] + "…"


def filterDocumentHits(
    hits: list[dict[str, Any]],
    catalogById: dict[str, DocumentCatalog],
    relationsByDoc: dict[str, set[tuple[str, str]]],
    *,
    documentTypes: list[str] | None,
    entityKeys: list[str] | None,
    effectiveAt: date | None,
) -> list[dict[str, Any]]:
    """编排放大过滤（纯函数，返回新列表）：见模块 docstring。"""
    requestedTypes = {t.upper() for t in (documentTypes or []) if t}
    requestedEntities = {
        tuple(part.strip().upper() for part in ek.split(":", 1))
        for ek in (entityKeys or [])
        if ek and ":" in ek
    }
    kept: list[dict[str, Any]] = []
    for hit in hits:
        catalog = catalogById.get(hit.get("document_id") or "")
        if catalog is None:
            continue  # fail-closed：密级/有效期无法核验
        if catalog.status != DocumentStatus.ACTIVE:
            continue  # 过期/归档版本不返回
        if requestedTypes and catalog.document_type.value.upper() not in requestedTypes:
            continue
        if requestedEntities:
            rels = relationsByDoc.get(catalog.document_id, set())
            if not (rels & requestedEntities):
                continue
        if (
            effectiveAt is not None
            and catalog.effective_date is not None
            and catalog.effective_date > effectiveAt
        ):
            continue  # 未来生效 ⇒ 剔除；生效期未知 ⇒ 保留（无法判定不误杀）
        kept.append(hit)
    return kept


def buildDocumentEvidence(
    hit: dict[str, Any], catalog: DocumentCatalog
) -> KnowledgeEvidenceRead:
    """chunk 命中 + catalog 全行 → M2-1 契约证据（security/sourceRef/validAt 回填）。"""
    distance = hit.get("distance")
    confidence = (
        round(max(0.0, min(1.0, 1.0 - float(distance))), 4)
        if isinstance(distance, (int, float))
        else None
    )
    validAt = (
        datetime(catalog.effective_date.year, catalog.effective_date.month,
                 catalog.effective_date.day)
        if catalog.effective_date is not None
        else None
    )
    return KnowledgeEvidenceRead(
        channel=KNOWLEDGE_CHANNEL_RAG,
        kind="DOCUMENT",
        ref=catalog.document_id,
        title=catalog.document_name,
        snippet=_clip(str(hit.get("chunk_text") or "")) or None,
        release_id=None,
        release_version=None,
        source=KNOWLEDGE_SOURCE_LIVE,
        confidence=confidence,
        valid_at=validAt,
        security=KnowledgeEvidenceSecurity(
            level=catalog.security_level.value,
            scope=(),
        ),
        source_ref=KnowledgeEvidenceSourceRef(
            system=None,
            object=None,
            record_key=None,
            document_id=catalog.document_id,
            query_id=None,
        ),
    )


async def searchProcurementDocuments(
    session: AsyncSession,
    user: Any,
    *,
    query: str,
    documentTypes: list[str] | None = None,
    entityKeys: list[str] | None = None,
    effectiveAt: date | None = None,
    topK: int = 5,
    searchFn: Callable[..., Awaitable[list[dict]]] | None = None,
) -> list[KnowledgeEvidenceRead]:
    """文档检索编排：RagService 检索（含密级闸）→ 编排过滤 → M2-1 契约证据。

    `searchFn` 为测试注入点（默认 `RagService().searchDocuments`）。
    """
    if not query.strip():
        return []
    search = searchFn if searchFn is not None else RagService().searchDocuments
    try:
        hits = await search(
            query, user=user, session=session, top_k=max(1, min(topK, 50))
        )
    except Exception:
        logger.warning("文档检索失败（RAG 链路异常）⇒ 空结果不阻断", exc_info=True)
        return []
    if not hits:
        return []

    uniqueIds = list({h.get("document_id") for h in hits if h.get("document_id")})
    catalogRows = (
        await session.execute(
            select(DocumentCatalog).where(DocumentCatalog.document_id.in_(uniqueIds))
        )
    ).scalars().all()
    catalogById = {row.document_id: row for row in catalogRows}
    relationsByDoc: dict[str, set[tuple[str, str]]] = {}
    if entityKeys:
        relationRows = (
            await session.execute(
                select(DocumentEntityRelation).where(
                    DocumentEntityRelation.document_id.in_(uniqueIds)
                )
            )
        ).scalars().all()
        for row in relationRows:
            relationsByDoc.setdefault(row.document_id, set()).add(
                (str(row.entity_type).upper(), str(row.entity_key))
            )

    kept = filterDocumentHits(
        hits,
        catalogById,
        relationsByDoc,
        documentTypes=documentTypes,
        entityKeys=entityKeys,
        effectiveAt=effectiveAt,
    )
    return [
        buildDocumentEvidence(hit, catalogById[hit["document_id"]])
        for hit in kept
    ]
