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

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser, getDb
from app.domain.enums import DocumentStatus, EntityType
from app.domain.schemas import (
    DocEntityRelationCreate,
    DocEntityRelationRead,
    DocumentCreate,
    DocumentRead,
    DocumentUpdate,
)
from app.services.document_service import DocumentService

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
    doc = await service.createDocument(session, payload)
    return DocumentRead.model_validate(doc)


@router.put("/{documentId:int}", response_model=DocumentRead)
async def updateDocument(
    documentId: int,
    payload: DocumentUpdate,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> DocumentRead:
    doc = await service.updateDocument(session, documentId, payload)
    return DocumentRead.model_validate(doc)


@router.delete("/{documentId:int}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteDocument(
    documentId: int,
    _user=Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DocumentService = Depends(getDocumentService),
) -> None:
    await service.deleteDocument(session, documentId)


# ---------------------------------------------------------------------------
# Document-Entity Relations
# ---------------------------------------------------------------------------


@router.get("/relations", response_model=list[DocEntityRelationRead])
async def listRelations(
    document_id: str | None = Query(default=None, alias="documentId"),
    entity_type: EntityType | None = Query(default=None, alias="entityType"),
    entity_key: int | None = Query(default=None, alias="entityKey", ge=1),
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
    from app.services.rag_service import RagService, RagError

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
        )
        return result
    except RagError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/search")
async def searchDocuments(
    q: str = Query(..., min_length=1),
    security_level: str | None = Query(default=None, alias="securityLevel"),
    top_k: int = Query(default=5, ge=1, le=50, alias="topK"),
    _user=Depends(getCurrentUser),
) -> list[dict]:
    """语义检索文档 chunks。"""
    from app.services.rag_service import RagService, RagError

    svc = RagService()
    try:
        return await svc.searchDocuments(
            q,
            security_level=security_level,
            top_k=top_k,
        )
    except RagError as e:
        raise HTTPException(status_code=422, detail=str(e))
