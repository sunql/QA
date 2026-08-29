"""模型配置 CRUD 服务。

负责 LlmConfig 的创建/查询/更新/停用，并对 API Key 进行加密存储。
查询接口不返回密钥明文。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import LlmConfig
from app.domain.schemas import LlmConfigCreate, LlmConfigUpdate
from app.infrastructure.security.crypto import encryptApiKey
from app.services.messages_zh import (
    MSG_MODEL_CONFIG_NAME_EXISTS,
    MSG_MODEL_CONFIG_NOT_FOUND,
)

logger = logging.getLogger(__name__)


class ModelConfigService:
    """模型配置服务。"""

    async def create(self, session: AsyncSession, dto: LlmConfigCreate) -> LlmConfig:
        """创建模型配置。model_name 唯一。"""
        existing = await session.execute(
            select(LlmConfig).where(LlmConfig.model_name == dto.model_name)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(MSG_MODEL_CONFIG_NAME_EXISTS.format(name=dto.model_name))
        config = LlmConfig(
            model_name=dto.model_name,
            provider=dto.provider,
            api_endpoint=dto.api_endpoint,
            api_key_encrypted=encryptApiKey(dto.api_key) if dto.api_key else None,
            cost_per_1k_input=dto.cost_per_1k_input,
            cost_per_1k_output=dto.cost_per_1k_output,
            max_input_tokens=dto.max_input_tokens,
            weight=dto.weight,
            cost_threshold=dto.cost_threshold,
            is_active=dto.is_active,
        )
        session.add(config)
        await session.commit()
        await session.refresh(config)
        logger.info("创建模型配置 id=%s name=%s provider=%s", config.id, config.model_name, config.provider)
        return config

    async def list(self, session: AsyncSession, *, activeOnly: bool = False) -> list[LlmConfig]:
        """列出模型配置。activeOnly=True 仅返回启用的。"""
        stmt = select(LlmConfig).order_by(LlmConfig.id)
        if activeOnly:
            stmt = stmt.where(LlmConfig.is_active.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, session: AsyncSession, configId: int) -> LlmConfig:
        """按 id 获取，不存在抛 NotFoundError。"""
        config = await session.get(LlmConfig, configId)
        if config is None:
            raise NotFoundError(MSG_MODEL_CONFIG_NOT_FOUND.format(id=configId))
        return config

    async def update(
        self, session: AsyncSession, configId: int, dto: LlmConfigUpdate
    ) -> LlmConfig:
        """部分更新。api_key 若提供则重新加密。"""
        config = await self.get(session, configId)
        updates = dto.model_dump(exclude_unset=True, exclude_none=True)
        if "api_key" in updates:
            plainKey = updates.pop("api_key")
            config.api_key_encrypted = encryptApiKey(plainKey) if plainKey else None
        for field, value in updates.items():
            setattr(config, field, value)
        await session.commit()
        await session.refresh(config)
        logger.info("更新模型配置 id=%s fields=%s", configId, list(updates.keys()))
        return config

    async def deactivate(self, session: AsyncSession, configId: int) -> None:
        """软删除：停用模型配置（is_active=False）。"""
        config = await self.get(session, configId)
        config.is_active = False
        await session.commit()
        logger.info("停用模型配置 id=%s", configId)
