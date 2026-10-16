"""模型配置 CRUD 服务。

负责 LlmConfig 的创建/查询/更新/停用，并对 API Key 进行加密存储。
查询接口不返回密钥明文。

X1 防御纵深（QA-1012-01 #1）：create/update/deactivate 接受可选 ``actor`` 参数。
传 ``None`` 时维持旧行为（向后兼容内部调用）；传 ``CurrentUser`` 时校验
``"admin" in actor.roles``，否则抛 ``PermissionDeniedError``。即使绕开 API 路由层
``Depends(requireAdmin)`` 直接调 service，也无法越权落库。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.domain.models import LlmConfig
from app.domain.schemas import LlmConfigCreate, LlmConfigUpdate
from app.infrastructure.llm import factory as llmFactory
from app.infrastructure.llm.factory import invalidateClient
from app.infrastructure.security.crypto import encryptApiKey
from app.infrastructure.url_validator import validateEndpointUrl
from app.services.messages_zh import (
    MSG_MODEL_CONFIG_NAME_EXISTS,
    MSG_MODEL_CONFIG_NOT_FOUND,
)

logger = logging.getLogger(__name__)

# X1：与 app.dependencies.requireAdmin 共享角色判定字符串。
_ADMIN_ROLE = "admin"


def _requireAdminActor(actor: CurrentUser | None) -> None:
    """X1 防御纵深：actor 传 None 时跳过（向后兼容内部调用），否则强制 admin 角色。

    Raises:
        PermissionDeniedError: actor 非 None 且不含 admin 角色。
    """
    if actor is None:
        return  # 内部调用 / 测试 fixture 场景：显式 opt-out
    if _ADMIN_ROLE not in (actor.roles or ()):
        raise PermissionDeniedError("需要 admin 角色")


class ModelConfigService:
    """模型配置服务。"""

    async def create(
        self,
        session: AsyncSession,
        dto: LlmConfigCreate,
        *,
        actor: CurrentUser | None = None,
    ) -> LlmConfig:
        """创建模型配置。model_name 唯一；endpoint 须符合生产 URL 规范。

        X1 防御纵深：传 ``actor`` 时强制 admin 角色；``actor=None`` 维持旧行为
        （向后兼容内部调用与已有测试 fixture）。
        """
        _requireAdminActor(actor)
        # audit-fx-1.1 fix1：创建时即 fail-fast，防止无效 URL 入库
        if dto.api_endpoint:
            try:
                validateEndpointUrl(dto.api_endpoint)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
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
            temperature=dto.temperature,
            disable_thinking=dto.disable_thinking,
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
        self,
        session: AsyncSession,
        configId: int,
        dto: LlmConfigUpdate,
        *,
        actor: CurrentUser | None = None,
    ) -> LlmConfig:
        """部分更新。api_key 若提供则重新加密。变更 endpoint 必须同时提供新 key。

        X1 防御纵深：传 ``actor`` 时强制 admin 角色；``actor=None`` 维持旧行为。
        """
        _requireAdminActor(actor)
        config = await self.get(session, configId)
        updates = dto.model_dump(exclude_unset=True, exclude_none=True)

        # audit-fx-1.1：endpoint 变更必须同时提供新 key，防止旧 key 泄露到新端点
        if "api_endpoint" in updates and updates["api_endpoint"] != config.api_endpoint:
            if not updates.get("api_key"):
                raise ValidationError("apiEndpoint 变更时必须同时提供新的 apiKey")
            validateEndpointUrl(updates["api_endpoint"])
            plainKey = updates.pop("api_key")
            config.api_key_encrypted = encryptApiKey(plainKey) if plainKey else None

        if "api_key" in updates:
            plainKey = updates.pop("api_key")
            config.api_key_encrypted = encryptApiKey(plainKey) if plainKey else None
        for field, value in updates.items():
            setattr(config, field, value)
        await session.commit()
        await session.refresh(config)
        # 客户端按 config id 缓存且构造时固化 endpoint/key——编辑后必须失效，
        # 否则运行中的进程永远用旧配置（2026-10-03 MiniMax Connection error 根因）
        await invalidateClient(configId)
        logger.info("更新模型配置 id=%s fields=%s", configId, list(updates.keys()))
        # audit-fx-2.2: belt-and-suspenders — 更新后显式 evict factory 缓存
        llmFactory.evictClient(configId)
        return config

    async def deactivate(
        self,
        session: AsyncSession,
        configId: int,
        *,
        actor: CurrentUser | None = None,
    ) -> None:
        """软删除：停用模型配置（is_active=False）。

        X1 防御纵深：传 ``actor`` 时强制 admin 角色；``actor=None`` 维持旧行为。
        """
        _requireAdminActor(actor)
        config = await self.get(session, configId)
        config.is_active = False
        await session.commit()
        await invalidateClient(configId)
        logger.info("停用模型配置 id=%s", configId)
