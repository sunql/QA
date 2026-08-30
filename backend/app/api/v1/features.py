"""AI Feature Layer REST API（Phase 4.3）。

挂在 /api/v1/features：定义 CRUD + 特征值查询 + 单/批量计算。
与 entity_mapping / kpi_catalog 同模式：扁平路径 + Depends(getCurrentUser) 鉴权。
Feature 不进 Neo4j / Milvus（预计算快照，不参与本体检索）。

ACL：PUT / DELETE 走 AclService（owner-based），PermissionDenied → 403（service 层抛）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    FeatureComputeBatchResult,
    FeatureComputeResult,
    FeatureDefinitionCreate,
    FeatureDefinitionRead,
    FeatureDefinitionUpdate,
    FeatureValueRead,
)
from app.services.feature_compute_service import FeatureComputeService
from app.services.feature_definition_service import FeatureDefinitionService

router = APIRouter(dependencies=[])
_service = FeatureDefinitionService()
_computeService = FeatureComputeService()


def getFeatureDefinitionService() -> FeatureDefinitionService:
    """service 无状态依赖，直接返回新实例；保留工厂风格便于后续注入。"""
    return _service


def getFeatureComputeService() -> FeatureComputeService:
    """compute service 依赖；测试可 monkeypatch 其 _adapterProvider。"""
    return _computeService


@router.get("", response_model=list[FeatureDefinitionRead], status_code=status.HTTP_200_OK)
async def listFeatures(
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureDefinitionService = Depends(getFeatureDefinitionService),
) -> list[FeatureDefinitionRead]:
    """列出所有特征定义（按 feature_name 升序）。"""
    return [FeatureDefinitionRead.model_validate(f) for f in await service.listFeatures(db)]


@router.post("", response_model=FeatureDefinitionRead, status_code=status.HTTP_201_CREATED)
async def createFeature(
    dto: FeatureDefinitionCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureDefinitionService = Depends(getFeatureDefinitionService),
) -> FeatureDefinitionRead:
    """创建特征定义。重复 feature_name → 409；calculation_logic 非只读 → 400。"""
    return FeatureDefinitionRead.model_validate(await service.createFeature(db, dto, user))


@router.get("/{featureId}", response_model=FeatureDefinitionRead, status_code=status.HTTP_200_OK)
async def getFeature(
    featureId: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureDefinitionService = Depends(getFeatureDefinitionService),
) -> FeatureDefinitionRead:
    """按 ID 查询特征定义。"""
    return FeatureDefinitionRead.model_validate(await service.getFeature(db, featureId))


@router.put("/{featureId}", response_model=FeatureDefinitionRead, status_code=status.HTTP_200_OK)
async def updateFeature(
    featureId: int,
    dto: FeatureDefinitionUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureDefinitionService = Depends(getFeatureDefinitionService),
) -> FeatureDefinitionRead:
    """更新特征定义。owner 不匹配 → 403。"""
    return FeatureDefinitionRead.model_validate(await service.updateFeature(db, featureId, dto, user))


@router.delete("/{featureId}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteFeature(
    featureId: int,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureDefinitionService = Depends(getFeatureDefinitionService),
) -> None:
    """删除特征定义（级联删 feature_value）。owner 不匹配 → 403。"""
    await service.deleteFeature(db, featureId, user)


@router.post("/compute-batch", response_model=FeatureComputeBatchResult, status_code=status.HTTP_200_OK)
async def computeBatch(
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureComputeService = Depends(getFeatureComputeService),
) -> FeatureComputeBatchResult:
    """批量计算所有 is_enabled=true 且 status=ACTIVE 的特征。"""
    results = await service.computeAllEnabled(db)
    return FeatureComputeBatchResult(
        results=results,
        total_rows=sum(r.rows for r in results),
    )


@router.post("/{featureId}/compute", response_model=FeatureComputeResult, status_code=status.HTTP_200_OK)
async def computeFeature(
    featureId: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: FeatureComputeService = Depends(getFeatureComputeService),
) -> FeatureComputeResult:
    """触发单个特征计算，幂等 upsert 结果。"""
    rows = await service.computeFeatureById(db, featureId)
    return FeatureComputeResult(feature_id=featureId, rows=rows)


@router.get("/{featureId}/values", response_model=list[FeatureValueRead], status_code=status.HTTP_200_OK)
async def listFeatureValues(
    featureId: int,
    _user: CurrentUser = Depends(getCurrentUser),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(getDb),
    service: FeatureDefinitionService = Depends(getFeatureDefinitionService),
) -> list[FeatureValueRead]:
    """列出某特征的特征值（分页，验证用）。"""
    values = await service.listValues(db, featureId, limit=limit, offset=offset)
    return [FeatureValueRead.model_validate(v) for v in values]
