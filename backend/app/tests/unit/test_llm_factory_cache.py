"""LLM 客户端工厂缓存失效测试。

TDD: RED(2 tests) -> GREEN(factory.py + model_config_service.py)
覆盖：
- cache_invalidated_on_endpoint_update: 更新 endpoint 后 cache miss，返回新客户端
- cache_returns_same_object_if_unchanged: 配置未变时返回同一客户端
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.infrastructure.llm import factory as llmFactory
from app.infrastructure.security.crypto import encryptApiKey


@pytest.fixture
async def modelConfig(dbSession: AsyncSession) -> LlmConfig:
    """创建一个有效的 LlmConfig 行，供缓存测试使用。

    使用真实 api_endpoint 和加密后的 api_key。
    """
    cfg = LlmConfig(
        model_name="test-cache-model",
        provider="openai",
        api_endpoint="https://api.openai.com/v1",
        api_key_encrypted=encryptApiKey("sk-test-factory-cache-key"),
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.002,
        max_input_tokens=8000,
        weight=1.0,
        cost_threshold=10.0,
        is_active=True,
    )
    dbSession.add(cfg)
    await dbSession.commit()
    await dbSession.refresh(cfg)
    # 重置工厂缓存，确保测试隔离
    llmFactory.resetFactory()
    yield cfg
    # teardown: 清工厂缓存
    llmFactory.resetFactory()


@pytest.mark.asyncio
async def test_cache_invalidated_on_endpoint_update(dbSession: AsyncSession, modelConfig: LlmConfig):
    """endpoint 更新后，cache miss，新客户端指向新 endpoint。"""
    # 第一次调用：缓存命中
    client1 = await llmFactory.getOrCreateClient(modelConfig.id, session=dbSession)
    assert client1 is not None

    # 显式更新 updated_at（确保 cache key 变化）
    original_updated_at = modelConfig.updated_time
    modelConfig.api_endpoint = "https://api.anthropic.com/v1"
    modelConfig.updated_time = datetime.now(timezone.utc)
    await dbSession.commit()

    # 第二次调用：cache key 变化，应返回新客户端
    client2 = await llmFactory.getOrCreateClient(modelConfig.id, session=dbSession)
    assert client1 is not client2, "更新 endpoint 后应 cache miss，返回新实例"
    assert client2._apiEndpoint == "https://api.anthropic.com/v1", "新客户端应指向新 endpoint"
    # 确认 updated_time 确实变了（防止测试本身的问题）
    assert modelConfig.updated_time > original_updated_at


@pytest.mark.asyncio
async def test_cache_returns_same_object_if_unchanged(
    dbSession: AsyncSession,
    modelConfig: LlmConfig,
):
    """配置未变时，同一 configId 两次调用返回同一客户端实例。"""
    c1 = await llmFactory.getOrCreateClient(modelConfig.id, session=dbSession)
    c2 = await llmFactory.getOrCreateClient(modelConfig.id, session=dbSession)
    assert c1 is c2, "配置未变时 LRU cache 应返回同一实例"
