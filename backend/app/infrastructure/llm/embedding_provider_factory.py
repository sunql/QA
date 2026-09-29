"""Embedding 服务运行时解析（resolver）。

从激活的 embedding_provider 解析出 EmbeddingClient，带缓存与失效。

优先级：激活的 provider > 环境变量。未激活任何 provider（或 DB 查询失败）时回退到
环境变量客户端，保持「未启用 registry」部署的既有行为。

维度守卫：**两条解析路径共用**（registry 的 dimension 列 / env 的 EMBEDDING_DIMENSION
声明）。声明的输出维度与 Milvus 集合维度不一致时 fail-fast 抛 ConfigError，提示重建
集合 + 回填（scripts/backfill_milvus_embeddings.py），避免静默插入维度错误数据。
env 路径**未声明**维度时只记 warning（模型名→维度无可靠映射，猜错会拦下正确部署）。

provider_type 守卫（H7）：取值须在 KNOWN_PROVIDER_TYPES 内，否则 fail-fast，不再静默
按 OpenAI 兼容处理。**客户端构造目前只实现 OpenAI 兼容形态**（/v1/embeddings）——已知
类型之间不做分支：ollama / omlx / 兼容代理都经各自的 OpenAI 兼容端点接入，真正需要
原生协议（如 Ollama `/api/embeddings`）时再按类型扩展，本批不预先发明未验证的协议。

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
from app.services.messages_zh import (
    MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH,
    MSG_EMBEDDING_PROVIDER_TYPE_UNKNOWN,
)

logger = logging.getLogger(__name__)

# H7：已知 provider_type 集合。SSOT 与前端 `frontend/src/types/embeddingProvider.ts` 的
# `EmbeddingProviderType` 联合类型一致 —— 扩展类型须两侧同步（前端下拉 + 这里）。
KNOWN_PROVIDER_TYPES: tuple[str, ...] = ("ollama", "omlx", "openai_compatible")

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
        _assertEnvFallbackDimension()
        client = EmbeddingClient()
        _activeClient = client
        logger.info(
            "无激活 embedding provider，回退环境变量模型 %s", getSettings().embeddingModel
        )
        return client
    _assertProviderTypeKnown(provider)
    _assertDimensionMatches(
        name=provider["name"],
        declaredDimension=provider["dimension"],
        source="provider 注册表",
    )
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
        "provider_type": provider.provider_type,
        "base_url": provider.base_url,
        "model_name": provider.model_name,
        "api_key_encrypted": provider.api_key_encrypted,
        "dimension": provider.dimension,
    }


def _assertProviderTypeKnown(provider: dict) -> None:
    """provider_type 守卫（H7）：未知取值 fail-fast，不再静默按 OpenAI 兼容处理。

    此前该列是**死元数据**（工厂从不读）。代价是配置写错、或将来接入原生协议的服务时，
    表现为难以定位的调用错误。这里把「只支持这些」显式化：值不在已知集合内、或为空
    （列非空但历史/直写可能留空）都拒绝，消息给出被拒值与已知集合。
    """
    providerType = (provider.get("provider_type") or "").strip()
    if providerType in KNOWN_PROVIDER_TYPES:
        return
    raise ConfigError(
        MSG_EMBEDDING_PROVIDER_TYPE_UNKNOWN.format(
            id=provider["id"],
            name=provider["name"],
            providerType=providerType or "(空)",
            known=", ".join(KNOWN_PROVIDER_TYPES),
        )
    )


def _assertDimensionMatches(
    *, name: str, declaredDimension: int, source: str
) -> None:
    """维度守卫：**声明的**输出维度必须与 Milvus 集合一致，否则 fail-fast。

    两条解析路径共用：registry 路径的声明来自 `embedding_provider.dimension` 列，
    env 回退路径来自 `EMBEDDING_DIMENSION`（见 `_assertEnvFallbackDimension`）。
    """
    milvusDimension = milvus.getEmbeddingDimension()
    if declaredDimension != milvusDimension:
        raise ConfigError(
            MSG_EMBEDDING_PROVIDER_DIMENSION_MISMATCH.format(
                name=name,
                source=source,
                dimension=declaredDimension,
                milvusDimension=milvusDimension,
            )
        )


def _assertEnvFallbackDimension() -> None:
    """env 回退路径的维度守卫（H7）。

    与 registry 路径的关键差别：env 路径**没有**声明维度的元数据 —— 只有一个模型名，
    而「模型名 → 输出维度」没有可靠映射（同名不同版本、代理端点改写都可能不同）。故分两档：

    - 部署方用 `EMBEDDING_DIMENSION` 声明了 → 与 Milvus 集合比对，不一致 fail-fast；
    - 未声明 → 记 warning，把「这条路没有守卫」**显式说出来**（此前是直接 return，
      既无校验也无痕迹）。不猜测模型维度：猜错会拦下正确部署，比不猜更糟；这是本路径的
      已知残留，SSOT 已登记。
    """
    settings = getSettings()
    declaredDimension = settings.embeddingDimension
    if declaredDimension is None:
        logger.warning(
            "env 回退 embedding 路径未声明 EMBEDDING_DIMENSION，跳过维度守卫（模型 %s）；"
            "输出维度与 Milvus 集合 %d 不一致时只能在写入/检索侧报错",
            settings.embeddingModel,
            milvus.getEmbeddingDimension(),
        )
        return
    _assertDimensionMatches(
        name=settings.embeddingModel,
        declaredDimension=declaredDimension,
        source="env 回退（EMBEDDING_DIMENSION）",
    )
