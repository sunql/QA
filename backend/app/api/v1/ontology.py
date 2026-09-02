"""本体（Ontology）管理 API。

Class / Property / Metric CRUD + Milvus 语义检索：
- GET/POST       /ontology/classes
- GET/PUT/DELETE /ontology/classes/{id}
- GET/POST       /ontology/properties
- GET/PUT/DELETE /ontology/properties/{id}
- GET/POST       /ontology/metrics
- GET/PUT/DELETE /ontology/metrics/{id}
- POST           /ontology/embeddings/sync
- GET            /ontology/search?q=
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.error_messages import (
    MSG_PARAM_EMBEDDING_VECTOR,
    MSG_PARAM_INCLUDE_EXPIRED_ONTOLOGY,
    MSG_PARAM_ONTOLOGY_PG_PRIMARY_KEY,
    MSG_PARAM_SEARCH_ENTITY_TYPE,
    MSG_PARAM_SEARCH_QUERY,
    MSG_PARAM_SEARCH_TOP_K,
)
from app.domain.schemas import (
    OntologyClassCreate,
    OntologyClassRead,
    OntologyClassUpdate,
    OntologyJoinCreate,
    OntologyJoinRead,
    OntologyJoinUpdate,
    OntologyMetricCreate,
    OntologyMetricRead,
    OntologyMetricUpdate,
    OntologyPropertyCreate,
    OntologyPropertyRead,
    OntologyPropertyUpdate,
    OntologySearchResult,
)
from app.services.embedding_service import EmbeddingService
from app.services.ontology_service import OntologyService

router = APIRouter(prefix="/ontology", tags=["ontology"])
# 模块级 EmbeddingService：与 chat 模块同构，由 main.shutdownCleanup 统一关闭
_embeddingService = EmbeddingService()
_ontologyService = OntologyService(embeddingService=_embeddingService)


# =============================================================================
# Class
# =============================================================================


@router.get("/classes", response_model=list[OntologyClassRead], status_code=status.HTTP_200_OK)
async def listClasses(
    includeExpired: bool = Query(
        False, description=MSG_PARAM_INCLUDE_EXPIRED_ONTOLOGY
    ),
    db: AsyncSession = Depends(getDb),
) -> list[OntologyClassRead]:
    """列出本体类（Phase 6）。

    includeExpired=false（默认）：仅返回未删除的类（valid_to IS NULL）。
    includeExpired=true：额外返回软删除墓碑（版本管理移除后兼容保留）。
    """
    entities = await _ontologyService.listClasses(db, includeExpired=includeExpired)
    return [OntologyClassRead.model_validate(e) for e in entities]


@router.post("/classes", response_model=OntologyClassRead, status_code=status.HTTP_201_CREATED)
async def createClass(
    dto: OntologyClassCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyClassRead:
    """创建本体类（起始 version=1）。

    Phase 4.5：object_owner 由 service 从 actor.departments[0] 派生，DTO
    中不接受该字段（防越权声明）。
    """
    entity = await _ontologyService.createClass(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyClassRead.model_validate(entity)


@router.get(
    "/classes/{className}/versions",
    response_model=list[OntologyClassRead],
    status_code=status.HTTP_200_OK,
)
async def listClassVersions(
    className: str,
    db: AsyncSession = Depends(getDb),
) -> list[OntologyClassRead]:
    """按 class_name 列出该类的全部行（含软删除墓碑；版本管理移除后兼容保留）。"""
    entities = await _ontologyService.listVersions(db, className)
    return [OntologyClassRead.model_validate(e) for e in entities]


@router.get("/classes/{id}", response_model=OntologyClassRead, status_code=status.HTTP_200_OK)
async def getClass(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> OntologyClassRead:
    """按 ID 查询本体类。"""
    entity = await _ontologyService.getClass(db, id)
    return OntologyClassRead.model_validate(entity)


@router.put("/classes/{id}", response_model=OntologyClassRead, status_code=status.HTTP_200_OK)
async def updateClass(
    id: int,
    dto: OntologyClassUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyClassRead:
    """更新本体类（Phase 6：创建新版本而非原地修改，返回新 id）。

    Phase 4.5 扩展：走 owner-based ACL（object_owner 字段）。
    """
    entity = await _ontologyService.updateClass(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyClassRead.model_validate(entity)


@router.delete("/classes/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteClass(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """软删除本体类：valid_to = now()（墓碑），listClasses 默认不再返回。

    Phase 4.5 扩展：走 owner-based ACL。
    """
    await _ontologyService.deleteClass(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Property
# =============================================================================


@router.get(
    "/classes/{classId}/properties",
    response_model=list[OntologyPropertyRead],
    status_code=status.HTTP_200_OK,
)
async def listPropertiesByClass(
    classId: int,
    db: AsyncSession = Depends(getDb),
) -> list[OntologyPropertyRead]:
    """列出某本体类的所有属性。"""
    entities = await _ontologyService.listPropertiesByClass(db, classId)
    return [OntologyPropertyRead.model_validate(e) for e in entities]


@router.post(
    "/properties",
    response_model=OntologyPropertyRead,
    status_code=status.HTTP_201_CREATED,
)
async def createProperty(
    dto: OntologyPropertyCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyPropertyRead:
    """创建本体属性。"""
    entity = await _ontologyService.createProperty(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyPropertyRead.model_validate(entity)


@router.get(
    "/properties/{id}",
    response_model=OntologyPropertyRead,
    status_code=status.HTTP_200_OK,
)
async def getProperty(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> OntologyPropertyRead:
    """按 ID 查询本体属性。"""
    entity = await _ontologyService.getProperty(db, id)
    return OntologyPropertyRead.model_validate(entity)


@router.put(
    "/properties/{id}",
    response_model=OntologyPropertyRead,
    status_code=status.HTTP_200_OK,
)
async def updateProperty(
    id: int,
    dto: OntologyPropertyUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyPropertyRead:
    """更新本体属性。"""
    entity = await _ontologyService.updateProperty(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyPropertyRead.model_validate(entity)


@router.delete("/properties/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteProperty(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除本体属性。"""
    await _ontologyService.deleteProperty(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Metric
# =============================================================================


@router.get("/metrics", response_model=list[OntologyMetricRead], status_code=status.HTTP_200_OK)
async def listMetrics(
    db: AsyncSession = Depends(getDb),
) -> list[OntologyMetricRead]:
    """列出所有本体指标。"""
    entities = await _ontologyService.listMetrics(db)
    return [OntologyMetricRead.model_validate(e) for e in entities]


@router.post("/metrics", response_model=OntologyMetricRead, status_code=status.HTTP_201_CREATED)
async def createMetric(
    dto: OntologyMetricCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyMetricRead:
    """创建本体指标。"""
    entity = await _ontologyService.createMetric(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyMetricRead.model_validate(entity)


@router.get("/metrics/{id}", response_model=OntologyMetricRead, status_code=status.HTTP_200_OK)
async def getMetric(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> OntologyMetricRead:
    """按 ID 查询本体指标。"""
    entity = await _ontologyService.getMetric(db, id)
    return OntologyMetricRead.model_validate(entity)


@router.put("/metrics/{id}", response_model=OntologyMetricRead, status_code=status.HTTP_200_OK)
async def updateMetric(
    id: int,
    dto: OntologyMetricUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyMetricRead:
    """更新本体指标。"""
    entity = await _ontologyService.updateMetric(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyMetricRead.model_validate(entity)


@router.delete("/metrics/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteMetric(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除本体指标。"""
    await _ontologyService.deleteMetric(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Join（关联关系目录）
# =============================================================================


@router.get("/joins", response_model=list[OntologyJoinRead], status_code=status.HTTP_200_OK)
async def listJoins(
    db: AsyncSession = Depends(getDb),
) -> list[OntologyJoinRead]:
    """列出全部 join 边（运行时 JOIN 生成的唯一真源）。"""
    entities = await _ontologyService.listJoins(db)
    return [OntologyJoinRead.model_validate(e) for e in entities]


@router.post("/joins", response_model=OntologyJoinRead, status_code=status.HTTP_201_CREATED)
async def createJoin(
    dto: OntologyJoinCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyJoinRead:
    """创建 join 边。"""
    entity = await _ontologyService.createJoin(
        db, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyJoinRead.model_validate(entity)


@router.put("/joins/{id}", response_model=OntologyJoinRead, status_code=status.HTTP_200_OK)
async def updateJoin(
    id: int,
    dto: OntologyJoinUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> OntologyJoinRead:
    """更新 join 边。"""
    entity = await _ontologyService.updateJoin(
        db, id, dto,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )
    return OntologyJoinRead.model_validate(entity)


@router.delete("/joins/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteJoin(
    id: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> None:
    """删除 join 边。"""
    await _ontologyService.deleteJoin(
        db, id,
        actor=user.userId,
        actor_departments=",".join(user.departments) if user.departments else None,
    )


# =============================================================================
# Semantic Search — Milvus
# =============================================================================


class EmbeddingSyncRequest(BaseModel):
    """embedding 同步请求体。向量由调用方通过 LLM 生成后传入。"""

    ontologyId: int = Field(..., description=MSG_PARAM_ONTOLOGY_PG_PRIMARY_KEY)
    type: str = Field(..., pattern=r"^(class|property|metric)$")
    name: str
    alias: str | None = None
    description: str | None = None
    embedding: list[float] = Field(..., description=MSG_PARAM_EMBEDDING_VECTOR)


@router.post("/embeddings/sync", status_code=status.HTTP_204_NO_CONTENT)
async def syncEmbedding(payload: EmbeddingSyncRequest) -> None:
    """将单条 embedding 同步到 Milvus（新建或覆盖）。"""
    _ontologyService.syncEmbedding(
        ontologyId=payload.ontologyId,
        type=payload.type,
        name=payload.name,
        alias=payload.alias,
        description=payload.description,
        embedding=payload.embedding,
    )


@router.get(
    "/search",
    response_model=list[OntologySearchResult],
    status_code=status.HTTP_200_OK,
)
async def searchOntology(
    q: str = Query(..., min_length=1, max_length=200, description=MSG_PARAM_SEARCH_QUERY),
    topK: int = Query(10, ge=1, le=50, description=MSG_PARAM_SEARCH_TOP_K),
    type: str | None = Query(
        None, pattern=r"^(class|property|metric)$", description=MSG_PARAM_SEARCH_ENTITY_TYPE
    ),
) -> list[OntologySearchResult]:
    """本体语义检索：关键词 -> embedding -> Milvus 近邻搜索。

    依赖 ontology_embeddings 集合中已同步的向量（通过 /embeddings/sync 写入）。
    """
    return await _ontologyService.searchByKeyword(q, topK=topK, typeFilter=type)
