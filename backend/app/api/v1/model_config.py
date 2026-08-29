"""模型配置 CRUD 路由。

GET    /api/v1/models                 列出
POST   /api/v1/models                 创建
GET    /api/v1/models/{configId}      获取
PUT    /api/v1/models/{configId}      更新
DELETE /api/v1/models/{configId}      停用（软删除）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser
from app.domain.error_messages import MSG_PARAM_ACTIVE_ONLY_MODELS
from app.domain.schemas import LlmConfigCreate, LlmConfigRead, LlmConfigUpdate
from app.infrastructure.database import getDb
from app.services.model_config_service import ModelConfigService

router = APIRouter(dependencies=[Depends(getCurrentUser)])


@router.get("", response_model=list[LlmConfigRead])
async def listModels(
    activeOnly: bool = Query(default=False, description=MSG_PARAM_ACTIVE_ONLY_MODELS),
    session: AsyncSession = Depends(getDb),
) -> list[LlmConfigRead]:
    svc = ModelConfigService()
    configs = await svc.list(session, activeOnly=activeOnly)
    return [LlmConfigRead.model_validate(c) for c in configs]


@router.post("", response_model=LlmConfigRead, status_code=201)
async def createModel(
    dto: LlmConfigCreate,
    session: AsyncSession = Depends(getDb),
) -> LlmConfigRead:
    svc = ModelConfigService()
    config = await svc.create(session, dto)
    return LlmConfigRead.model_validate(config)


@router.get("/{configId}", response_model=LlmConfigRead)
async def getModel(configId: int, session: AsyncSession = Depends(getDb)) -> LlmConfigRead:
    svc = ModelConfigService()
    config = await svc.get(session, configId)
    return LlmConfigRead.model_validate(config)


@router.put("/{configId}", response_model=LlmConfigRead)
async def updateModel(
    configId: int,
    dto: LlmConfigUpdate,
    session: AsyncSession = Depends(getDb),
) -> LlmConfigRead:
    svc = ModelConfigService()
    config = await svc.update(session, configId, dto)
    return LlmConfigRead.model_validate(config)


@router.delete("/{configId}", status_code=204)
async def deleteModel(configId: int, session: AsyncSession = Depends(getDb)) -> None:
    svc = ModelConfigService()
    await svc.deactivate(session, configId)
