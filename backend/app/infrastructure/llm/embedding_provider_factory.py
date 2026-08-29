"""Embedding 服务运行时解析（resolver）。

从激活的 embedding_provider 解析出 EmbeddingClient，带缓存与失效。

优先级：激活的 provider > 环境变量。未激活任何 provider（或 DB 查询失败）时回退到
环境变量客户端，保持「未启用 registry」部署的既有行为。

维度守卫：激活 provider 的 dimension 与 Milvus 集合维度不一致时 fail-fast 抛
ConfigError，提示重建集合 + 回填（scripts/backfill_milvus_embeddings.py），避免静默
插入维度错误数据。

失效：CRUD 变更（激活/更新/删除）后调用 invalidateEmbeddingClientCache()，使下次解析
强制重新查询。缓存命中不做快速路径比对——环境变量在进程生命周期内静态，改动只会经由
CRUD 显式失效。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import getSettings
from app.domain.exceptions import ConfigError
from app.domain.models import EmbeddingProvider
from app.infrastructure import milvus_client as milvus
from app.infrastructure.database import getSessionFactory
from app.infrastructure.llm.embedding_client import EmbeddingClient
from app.infrastructure.security.crypto import decryptApiKey
from app.services.messages_zh import MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH

logger = logging.getLogger(__name__)

_activeClient: EmbeddingClient | None = None


async def getActiveEmbeddingClient() -> EmbeddingClient:
    """返回当前激活的 embedding 客户端（缓存命中直接返回，否则解析并缓存）。

    无激活 provider 或 DB 不可用时回退到环境变量客户端（保持旧部署兼容）。
    维度不匹配时抛 ConfigError。
    """
    global _activeClient
    if _activeClient is not None:
        return _activeClient
    provider = await _fetchActiveProvider()
    if provider is None:
        client = EmbeddingClient()
        _activeClient = client
        logger.info(
            "无激活 embedding provider，回退环境变量模型 %s", getSettings().embeddingModel
        )
        return client
    _assertDimensionMatches(provider)
    apiKey = decryptApiKey(provider["api_key_encrypted"] or "")
    client = EmbeddingClient(
        model=provider["model_name"],
        apiBase=provider["base_url"],
        apiKey=apiKey,
    )
    _activeClient = client
    logger.info(
        "使用激活 embedding provider id=%s name=%s model=%s",
        provider["id"],
        provider["name"],
        provider["model_name"],
    )
    return client


async def invalidateEmbeddingClientCache() -> None:
    """清空缓存并关闭旧客户端（CRUD 变更后调用）。"""
    global _activeClient
    old = _activeClient
    _activeClient = None
    if old is not None:
        await old.close()


def resetEmbeddingClientCache() -> None:
    """清空缓存（测试用；不关闭客户端，避免跨测试事件循环误配）。"""
    global _activeClient
    _activeClient = None


async def _fetchActiveProvider() -> dict | None:
    """查询激活的 provider，返回快照字典（会话关闭后可安全使用）。

    错误分级：
    - OperationalError（连接抖动/超时）：记录警告并返回 None 降级环境变量，保证
      fire-and-forget 的查询存储路径不因元数据库抖动阻断问答主流程。
    - 其他 SQLAlchemyError（如缺表/权限 ProgrammingError）：属运维配置错误，
      记录 ERROR 并向上抛，避免「静默用错模型」掩盖未执行迁移等问题。
    """
    try:
        async with getSessionFactory()() as session:
            result = await session.execute(
                select(EmbeddingProvider).where(EmbeddingProvider.is_active.is_(True)).limit(1)
            )
            provider = result.scalars().first()
    except OperationalError as exc:
        logger.warning("查询激活 embedding provider 连接失败，回退环境变量: %s", exc)
        return None
    except SQLAlchemyError as exc:
        logger.error("查询激活 embedding provider 出错（非连接性错误，疑似缺表/权限）: %s", exc)
        raise
    if provider is None:
        return None
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "model_name": provider.model_name,
        "api_key_encrypted": provider.api_key_encrypted,
        "dimension": provider.dimension,
    }


def _assertDimensionMatches(provider: dict) -> None:
    """维度守卫：激活服务的输出维度必须与 Milvus 集合一致，否则 fail-fast。"""
    milvusDimension = milvus.getEmbeddingDimension()
    if provider["dimension"] != milvusDimension:
        raise ConfigError(
            MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH.format(
                name=provider["name"],
                dimension=provider["dimension"],
                milvusDimension=milvusDimension,
            )
        )
