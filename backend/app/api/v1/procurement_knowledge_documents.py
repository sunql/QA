"""M2-3 采购文档检索：`POST /api/v1/procurement/knowledge/documents/search`。

设计稿 §8.3：`query / documentTypes[] / entityKeys[] / effectiveAt / topK` ⇒
`list[KnowledgeEvidenceRead]`（kind=DOCUMENT，security/sourceRef/validAt 回填）。
检索链路复用 `RagService.searchDocuments`（Milvus + Task 4.3 密级闸）；
编排过滤与证据映射在 `services/knowledge/document_search`（可注入 searchFn）。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Response
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.knowledge_schemas import (
    MAX_QUESTION_LENGTH,
    KnowledgeEvidenceRead,
)
from app.domain.schemas import CamelModel
from app.services.knowledge.document_search import searchProcurementDocuments

router = APIRouter(tags=["procurement-knowledge"])


class ProcurementDocumentSearchRequest(CamelModel):
    """文档检索请求体（独立模型——知识查询基类的 channels/scope 等字段与本端点无关）。"""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    documentTypes: list[str] = Field(default_factory=list, max_length=10)
    entityKeys: list[str] = Field(default_factory=list, max_length=20)
    effectiveAt: date | None = None
    topK: int = Field(default=5, ge=1, le=20)


@router.post(
    "/documents/search",
    response_model=list[KnowledgeEvidenceRead],
)
async def searchProcurementKnowledgeDocuments(
    response: Response,
    body: ProcurementDocumentSearchRequest,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[KnowledgeEvidenceRead]:
    """采购文档检索：密级闸 + 类型/实体/生效期过滤，证据带完整溯源契约。"""
    response.headers["Cache-Control"] = "no-store"
    return await searchProcurementDocuments(
        session,
        user,
        query=body.question,
        documentTypes=body.documentTypes or None,
        entityKeys=body.entityKeys or None,
        effectiveAt=body.effectiveAt,
        topK=body.topK,
    )
