"""Embedding 服务注册表 CRUD 路由。

GET    /api/v1/embedding-providers                    列出
POST   /api/v1/embedding-providers                    创建
GET    /api/v1/embedding-providers/active             当前激活（无激活 404）
GET    /api/v1/embedding-providers/{providerId}       获取
PUT    /api/v1/embedding-providers/{providerId}       更新
POST   /api/v1/embedding-providers/{providerId}/activate   设为唯一激活
DELETE /api/v1/embedding-providers/{providerId}       停用（软删除）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser
from app.domain.exceptions import NotFoundError
from app.domain.schemas import (
    EmbeddingProviderCreate,
    EmbeddingProviderRead,
    EmbeddingProviderUpdate,
)
from app.infrastructure.database import getDb
from app.services.embedding_provider_service import EmbeddingProviderService
from app.services.messages_zh import MSG_EMBEDDING_PROVIDER_NOT_FOUND

router = APIRouter(dependencies=[Depends(getCurrentUser)])


@router.get("", response_model=list[EmbeddingProviderRead])
async def listProviders(
    session: AsyncSession = Depends(getDb),
) -> list[EmbeddingProviderRead]:
    svc = EmbeddingProviderService()
    providers = await svc.list(session)
    return [EmbeddingProviderRead.model_validate(p) for p in providers]


@router.post("", response_model=EmbeddingProviderRead, status_code=201)
async def createProvider(
    dto: EmbeddingProviderCreate,
    session: AsyncSession = Depends(getDb),
) -> EmbeddingProviderRead:
    svc = EmbeddingProviderService()
    provider = await svc.create(session, dto)
    return EmbeddingProviderRead.model_validate(provider)


# /active 必须声明在 /{providerId} 之前，否则被路径参数路由吞掉
@router.get("/active", response_model=EmbeddingProviderRead)
async def getActiveProvider(
    session: AsyncSession = Depends(getDb),
) -> EmbeddingProviderRead:
    svc = EmbeddingProviderService()
    provider = await svc.getActive(session)
    if provider is None:
        raise NotFoundError(MSG_EMBEDDING_PROVIDER_NOT_FOUND.format(id="(active)"))
    return EmbeddingProviderRead.model_validate(provider)


@router.get("/{providerId}", response_model=EmbeddingProviderRead)
async def getProvider(
    providerId: int, session: AsyncSession = Depends(getDb)
) -> EmbeddingProviderRead:
    svc = EmbeddingProviderService()
    provider = await svc.get(session, providerId)
    return EmbeddingProviderRead.model_validate(provider)


@router.put("/{providerId}", response_model=EmbeddingProviderRead)
async def updateProvider(
    providerId: int,
    dto: EmbeddingProviderUpdate,
    session: AsyncSession = Depends(getDb),
) -> EmbeddingProviderRead:
    svc = EmbeddingProviderService()
    provider = await svc.update(session, providerId, dto)
    return EmbeddingProviderRead.model_validate(provider)


@router.post("/{providerId}/activate", response_model=EmbeddingProviderRead)
async def activateProvider(
    providerId: int, session: AsyncSession = Depends(getDb)
) -> EmbeddingProviderRead:
    svc = EmbeddingProviderService()
    provider = await svc.activate(session, providerId)
    return EmbeddingProviderRead.model_validate(provider)


@router.delete("/{providerId}", status_code=204)
async def deleteProvider(
    providerId: int, session: AsyncSession = Depends(getDb)
) -> None:
    svc = EmbeddingProviderService()
    await svc.deactivate(session, providerId)
