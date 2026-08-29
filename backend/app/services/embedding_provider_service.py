"""Embedding 服务注册表 CRUD。

管理 embedding_provider 表（注册可用的 embedding 模型服务），维护「单活互斥」：
is_active 同一时刻至多一行 True（DB 层有部分唯一索引兜底，见迁移 0014）。
激活/更新/删除后调用 invalidateEmbeddingClientCache()，使运行时 resolver 下次解析
强制重新查询。

API Key 用 Fernet 加密存储（复用 crypto），响应与查询永不返回明文。
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import EmbeddingProvider
from app.domain.schemas import EmbeddingProviderCreate, EmbeddingProviderUpdate
from app.infrastructure.llm.embedding_provider_factory import invalidateEmbeddingClientCache
from app.infrastructure.security.crypto import encryptApiKey
from app.services.messages_zh import (
    MSG_EMBEDDING_PROVIDER_ACTIVE_CONFLICT,
    MSG_EMBEDDING_PROVIDER_NAME_EXISTS,
    MSG_EMBEDDING_PROVIDER_NOT_FOUND,
)

logger = logging.getLogger(__name__)


async def _commitOrConflict(session: AsyncSession, *, name: str | None = None) -> None:
    """提交并捕获并发唯一约束冲突（名称或单活激活），转为 422。

    IntegrityError 可能来自：并发同名创建/改名、并发激活两个服务。均属数据冲突，
    不应以裸 500 泄漏给客户端。
    """
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if name is not None:
            raise ValidationError(MSG_EMBEDDING_PROVIDER_NAME_EXISTS.format(name=name)) from exc
        raise ValidationError(MSG_EMBEDDING_PROVIDER_ACTIVE_CONFLICT) from exc


class EmbeddingProviderService:
    """Embedding 服务注册表服务。"""

    async def create(self, session: AsyncSession, dto: EmbeddingProviderCreate) -> EmbeddingProvider:
        """创建服务。name 唯一；is_active=True 时取消其他服务的激活标记。"""
        await _assertNameUnique(session, name=dto.name)
        if dto.is_active:
            await _clearOtherActives(session, keepId=None)
        provider = EmbeddingProvider(
            name=dto.name,
            provider_type=dto.provider_type,
            base_url=dto.base_url,
            model_name=dto.model_name,
            api_key_encrypted=encryptApiKey(dto.api_key) if dto.api_key else None,
            dimension=dto.dimension,
            is_active=dto.is_active,
        )
        session.add(provider)
        await _commitOrConflict(session, name=dto.name)
        await session.refresh(provider)
        await invalidateEmbeddingClientCache()
        logger.info(
            "创建 embedding 服务 id=%s name=%s type=%s active=%s",
            provider.id,
            provider.name,
            provider.provider_type,
            provider.is_active,
        )
        return provider

    async def list(self, session: AsyncSession) -> list[EmbeddingProvider]:
        """列出全部服务（按 id 升序）。"""
        result = await session.execute(select(EmbeddingProvider).order_by(EmbeddingProvider.id))
        return list(result.scalars().all())

    async def get(self, session: AsyncSession, providerId: int) -> EmbeddingProvider:
        """按 id 获取，不存在抛 NotFoundError。"""
        provider = await session.get(EmbeddingProvider, providerId)
        if provider is None:
            raise NotFoundError(MSG_EMBEDDING_PROVIDER_NOT_FOUND.format(id=providerId))
        return provider

    async def getActive(self, session: AsyncSession) -> EmbeddingProvider | None:
        """返回当前激活的服务；无激活时返回 None。"""
        result = await session.execute(
            select(EmbeddingProvider).where(EmbeddingProvider.is_active.is_(True)).limit(1)
        )
        return result.scalars().first()

    async def update(
        self, session: AsyncSession, providerId: int, dto: EmbeddingProviderUpdate
    ) -> EmbeddingProvider:
        """部分更新。api_key 提供时重新加密；激活标记为 True 时取消其他服务。"""
        provider = await self.get(session, providerId)
        if dto.name is not None and dto.name != provider.name:
            await _assertNameUnique(session, name=dto.name, excludeId=providerId)
        updates = dto.model_dump(exclude_unset=True, exclude_none=True)
        # 显式发送 api_key=null 也应视为「清除 key」（exclude_none 会把它丢掉，需单独还原）
        if "api_key" in dto.model_fields_set:
            updates["api_key"] = dto.api_key
        if "api_key" in updates:
            plainKey = updates.pop("api_key")
            provider.api_key_encrypted = encryptApiKey(plainKey) if plainKey else None
        for field, value in updates.items():
            setattr(provider, field, value)
        if provider.is_active:
            await _clearOtherActives(session, keepId=provider.id)
        await _commitOrConflict(session, name=dto.name if dto.name is not None else None)
        await session.refresh(provider)
        await invalidateEmbeddingClientCache()
        logger.info("更新 embedding 服务 id=%s fields=%s", providerId, list(updates.keys()))
        return provider

    async def deactivate(self, session: AsyncSession, providerId: int) -> None:
        """软删除：取消激活标记。"""
        provider = await self.get(session, providerId)
        provider.is_active = False
        await _commitOrConflict(session)
        await invalidateEmbeddingClientCache()
        logger.info("停用 embedding 服务 id=%s", providerId)

    async def activate(self, session: AsyncSession, providerId: int) -> EmbeddingProvider:
        """设为唯一激活：取消其他服务的激活标记后置位当前服务。"""
        provider = await self.get(session, providerId)
        await _clearOtherActives(session, keepId=provider.id)
        provider.is_active = True
        await _commitOrConflict(session)
        await session.refresh(provider)
        await invalidateEmbeddingClientCache()
        logger.info("激活 embedding 服务 id=%s name=%s", provider.id, provider.name)
        return provider


async def _assertNameUnique(
    session: AsyncSession, *, name: str, excludeId: int | None = None
) -> None:
    """校验 name 唯一（排除 excludeId 自身），冲突抛 422。"""
    stmt = select(EmbeddingProvider).where(EmbeddingProvider.name == name)
    if excludeId is not None:
        stmt = stmt.where(EmbeddingProvider.id != excludeId)
    existing = await session.execute(stmt)
    if existing.scalar_one_or_none() is not None:
        raise ValidationError(MSG_EMBEDDING_PROVIDER_NAME_EXISTS.format(name=name))


async def _clearOtherActives(session: AsyncSession, *, keepId: int | None) -> None:
    """取消其他服务的激活标记（单活互斥）。"""
    stmt = update(EmbeddingProvider).where(EmbeddingProvider.is_active.is_(True))
    if keepId is not None:
        stmt = stmt.where(EmbeddingProvider.id != keepId)
    await session.execute(stmt.values(is_active=False))
