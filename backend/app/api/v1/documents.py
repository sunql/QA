"""文档目录 API 路由（Phase 5.1+5.2）。

挂在 /api/v1/documents：
  GET    /api/v1/documents                          列表（过滤：type / security_level / status）
  GET    /api/v1/documents/{id}                    详情
  POST   /api/v1/documents                         创建（201）
  PUT    /api/v1/documents/{id}                   更新
  DELETE /api/v1/documents/{id}                    删除（204）

挂在 /api/v1/documents/relations：
  GET    /api/v1/documents/relations               关联列表
  GET    /api/v1/documents/relations/{id}         关联详情
  POST   /api/v1/documents/relations               创建关联（201）
  DELETE /api/v1/documents/relations/{id}          删除关联（204）
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser, getDb
from app.domain.enums import DocumentStatus
from app.domain.exceptions import DomainError
from app.domain.schemas import (
    BusinessObjectCodeType,
    DocEntityRelationCreate,
    DocEntityRelationRead,
    DocumentCreate,
    DocumentRead,
    DocumentUpdate,
    DocQaRequest,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.document_service import DocumentService
from app.services.messages_zh import MSG_DOCUMENT_SOURCE_STORE_FAILED

logger = logging.getLogger(__name__)

router = APIRouter()


def getDocumentService() -> DocumentService:
    return DocumentService()


# ---------------------------------------------------------------------------
# Document Catalog
# ---------------------------------------------------------------------------


@router.get("", response_model=list[DocumentRead])
async def listDocuments(
    document_type: str | None = Query(default=None, alias="documentType"),
    security_level: str | None = Query(default=None, alias="securityLevel"),
    doc_status: DocumentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> list[DocumentRead]:
    docs = await service.listDocuments(
        session,
        document_type=document_type,
        security_level=security_level,
        status=doc_status,
        limit=limit,
        offset=offset,
    )
    return [DocumentRead.model_validate(d) for d in docs]


@router.get("/{documentId:int}", response_model=DocumentRead)
async def getDocument(
    documentId: int,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> DocumentRead:
    doc = await service.getDocument(session, documentId)
    return DocumentRead.model_validate(doc)


@router.post("", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def createDocument(
    payload: DocumentCreate,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> DocumentRead:
    doc = await service.createDocument(session, payload, actor=_user)
    return DocumentRead.model_validate(doc)


@router.put("/{documentId:int}", response_model=DocumentRead)
async def updateDocument(
    documentId: int,
    payload: DocumentUpdate,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> DocumentRead:
    doc = await service.updateDocument(session, documentId, payload, actor=_user)
    return DocumentRead.model_validate(doc)


@router.delete("/{documentId:int}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteDocument(
    documentId: int,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> None:
    await service.deleteDocument(session, documentId, actor=_user)


# ---------------------------------------------------------------------------
# Document-Entity Relations
# ---------------------------------------------------------------------------


@router.get("/relations", response_model=list[DocEntityRelationRead])
async def listRelations(
    document_id: str | None = Query(default=None, alias="documentId"),
    entity_type: BusinessObjectCodeType | None = Query(default=None, alias="entityType"),
    entity_key: str | None = Query(default=None, alias="entityKey", min_length=1, max_length=100),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> list[DocEntityRelationRead]:
    rels = await service.listRelations(
        session,
        document_id=document_id,
        entity_type=entity_type,
        entity_key=entity_key,
        limit=limit,
        offset=offset,
    )
    return [DocEntityRelationRead.model_validate(r) for r in rels]


@router.get("/relations/{relationId:int}", response_model=DocEntityRelationRead)
async def getRelation(
    relationId: int,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> DocEntityRelationRead:
    rel = await service.getRelation(session, relationId)
    return DocEntityRelationRead.model_validate(rel)


@router.post("/relations", response_model=DocEntityRelationRead, status_code=status.HTTP_201_CREATED)
async def createRelation(
    payload: DocEntityRelationCreate,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> DocEntityRelationRead:
    rel = await service.createRelation(session, payload)
    return DocEntityRelationRead.model_validate(rel)


@router.delete("/relations/{relationId:int}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteRelation(
    relationId: int,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> None:
    await service.deleteRelation(session, relationId)


# ---------------------------------------------------------------------------
# RAG: Upload & Search
# ---------------------------------------------------------------------------


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def uploadDocument(
    file: UploadFile,
    document_type: str | None = Query(default=None, alias="documentType"),
    version: str = Query(default="v1.0"),
    owner: str = Query(default=""),
    effective_date: str | None = Query(default=None, alias="effectiveDate"),
    security_level: str = Query(default="L1", alias="securityLevel"),
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> dict:
    """上传文档并触发 RAG 向量化入库。

    文件类型支持：PDF、DOCX、TXT、MD。
    """
    from app.services.rag_service import RagError, RagService, RagSourceStoreError

    content = await file.read()
    mime = file.content_type or "application/octet-stream"
    fname = file.filename or "unknown"

    svc = RagService()
    try:
        result = await svc.ingestDocument(
            session,
            content=content,
            filename=fname,
            mime_type=mime,
            document_type=document_type or "OTHER",
            version=version,
            owner=owner,
            effective_date=effective_date,
            security_level=security_level,
            actor=_user,
        )
        return result
    except RagSourceStoreError:
        # 源文件留存是基础设施故障，处置动作是「稍后重试」，不是换文件/换格式。
        # 底层原因（MinIO 端点/凭据）进日志，响应只给可行动提示。
        logger.exception("文档上传源文件留存失败: filename=%s", fname)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_DOCUMENT_SOURCE_STORE_FAILED,
        ) from None
    except RagError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/search")
async def searchDocuments(
    q: str = Query(..., min_length=1),
    security_level: str | None = Query(default=None, alias="securityLevel"),
    top_k: int = Query(default=5, ge=1, le=50, alias="topK"),
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[dict]:
    """语义检索文档 chunks。"""
    from app.services.rag_service import RagService, RagError

    svc = RagService()
    try:
        return await svc.searchDocuments(
            q,
            security_level=security_level,
            top_k=top_k,
            session=session,
        )
    except RagError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/qa")
@limiter.limit(rateLimitValue)
async def docQa(
    request: Request,
    dto: DocQaRequest,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    """文档问答 SSE 流式端点。

    流程：ownership 守卫 → 加载 LlmConfigs → RagQaService.answer_stream →
    每个 StreamEvent 走 toSse 序列化。
    """
    from app.services.messages_zh import MSG_SESSION_NOT_OWNED
    from sqlalchemy import select
    from app.domain.models import SessionMessage
    from app.services.rag_qa_service import RagQaService
    from app.services.rag_service import RagError
    from app.services.stream_events import EVENT_ERROR, StreamEvent

    # ownership 守卫：session 不属于当前用户 → 422
    # 新 sessions 允许（count==0 → 无已有行）；已有行但 user_id 不匹配 → 拒绝
    if dto.session_id:
        stmt = (
            select(SessionMessage.user_id)
            .where(
                SessionMessage.session_id == dto.session_id,
                SessionMessage.channel == "doc_qa",
            )
            .limit(1)
        )
        result = await session.execute(stmt)
        existing_user_id = result.scalar()
        if existing_user_id is not None and existing_user_id != _user.userId:
            raise HTTPException(status_code=422, detail=MSG_SESSION_NOT_OWNED)

    # 加载可用模型
    from app.services.model_config_service import ModelConfigService
    configs = await ModelConfigService().list(session, activeOnly=True)

    svc = RagQaService()

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in svc.answer_stream(
                session, dto, actor=_user, configs=configs,
            ):
                yield event.toSse()
        except RagError as exc:
            yield StreamEvent(
                EVENT_ERROR,
                {"error": str(exc), "errorType": "LLM"},
            ).toSse()
        except DomainError as exc:
            yield StreamEvent(
                EVENT_ERROR,
                {"error": exc.message, "errorType": "DOMAIN", "detail": exc.detail},
            ).toSse()

    return StreamingResponse(
        eventSource(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
