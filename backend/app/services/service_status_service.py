"""核心依赖服务状态探测。

对系统运行所依赖的基础设施做真实连通性探测：
- PostgreSQL 元数据库：SELECT 1（async 引擎连接）
- Neo4j 图库：driver.verify_connectivity()
- Milvus 向量库：checkHealth()（list_collections）
- Embedding 服务：GET {base_url}/models

所有探测并发执行，单项挂不拖垮整体；每项带独立超时。
探测函数均为模块级函数（便于测试 monkeypatch），各自捕获异常并返回
ServiceStatus，不向上抛——因此 checkAll 的 gather 无需 return_exceptions。
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import getSettings
from app.domain.schemas import ServiceStatus, ServiceStatusResponse
from app.infrastructure import database, milvus_client, neo4j_client
from app.infrastructure.llm.embedding_provider_factory import getActiveEmbeddingClient
from app.infrastructure.neo4j_client import _sanitizeUri
from app.services.messages_zh import (
    MSG_EMBEDDING_NOT_CONFIGURED,
    MSG_SERVICE_CHECK_FAILED,
    MSG_SERVICE_CHECK_TIMEOUT,
)

logger = logging.getLogger(__name__)

# 单项探测的硬超时（秒），避免单个依赖挂起拖住整个状态页
SERVICE_CHECK_TIMEOUT_SECONDS = 5

# 阻塞式探测（Neo4j / Milvus SDK）专用线程池。
# to_thread 无法杀死线程：超时只中断 await，底层线程会继续跑完 SDK 自身超时。
# 独立小池把「挂死的探测线程」隔离在状态页内，不占满应用默认线程池
# （min(32, cpu+4)）导致其他 to_thread 调用饿死。
_PROBE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")


def _elapsedMs(start: float) -> int:
    """返回自 start 起的耗时（毫秒，向下取整到非负整数）。"""
    return max(0, round((time.perf_counter() - start) * 1000))


def _timeoutDetail() -> str:
    return MSG_SERVICE_CHECK_TIMEOUT


def _sanitizeEndpoint(endpoint: str) -> str:
    """脱敏展示端点：剥除 URL 内嵌凭据（userinfo）与查询/片段参数。

    兼容带 scheme（``http://user:pass@host:port``）与无 scheme
    （``user:pass@host:port``）两种形态；查询串可能携带 ``api_key`` 等密钥，
    一并剥除。日志中记录原始值，响应只返回脱敏结果。
    """
    stripped = endpoint.split("?", 1)[0].split("#", 1)[0]
    scheme_idx = stripped.find("://")
    auth_start = scheme_idx + 3 if scheme_idx != -1 else 0
    auth_end = stripped.find("/", auth_start)
    if auth_end == -1:
        auth_end = len(stripped)
    authority = stripped[auth_start:auth_end]
    at = authority.rfind("@")
    if at == -1:
        return stripped
    return stripped[:auth_start] + authority[at + 1 :] + stripped[auth_end:]


def _checkFailedDetail(exc: Exception) -> str:
    """探测失败详情：完整异常只写服务端日志，响应返回通用脱敏提示。

    原始异常消息可能含数据库用户名、内网主机、完整 URL 等敏感信息，不宜直出；
    状态（down）+ 端点已足以定位到具体依赖，根因到日志中查。
    """
    logger.error("Service health probe failed: %s", exc, exc_info=True)
    return MSG_SERVICE_CHECK_FAILED


def _neo4jEndpoint() -> str | None:
    """安全解析 Neo4j 展示端点；畸形 URI（如未闭合的 IPv6 字面量）抛异常时返回 None。"""
    try:
        return _sanitizeUri(getSettings().neo4jUri)
    except Exception as exc:
        logger.error("Failed to resolve Neo4j endpoint: %s", exc, exc_info=True)
        return None


async def _probeInThread(fn) -> None:
    """在专用探测线程池中执行阻塞探测，受 wait_for 超时约束。"""
    loop = asyncio.get_running_loop()
    await asyncio.wait_for(
        loop.run_in_executor(_PROBE_EXECUTOR, fn), timeout=SERVICE_CHECK_TIMEOUT_SECONDS
    )


async def _pingPostgres() -> None:
    engine = database.getEngine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


def _postgresEndpoint() -> str:
    return database.getEngine().url.render_as_string(hide_password=True)


async def _checkPostgres() -> ServiceStatus:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(_pingPostgres(), timeout=SERVICE_CHECK_TIMEOUT_SECONDS)
        return ServiceStatus(
            name="postgresql",
            status="up",
            latency_ms=_elapsedMs(start),
            endpoint=_postgresEndpoint(),
        )
    except TimeoutError:
        return ServiceStatus(
            name="postgresql",
            status="down",
            latency_ms=_elapsedMs(start),
            endpoint=_postgresEndpoint(),
            detail=_timeoutDetail(),
        )
    except Exception as exc:
        return ServiceStatus(
            name="postgresql",
            status="down",
            latency_ms=_elapsedMs(start),
            endpoint=_postgresEndpoint(),
            detail=_checkFailedDetail(exc),
        )


def _pingNeo4j() -> None:
    neo4j_client.getDriver().verify_connectivity()


async def _checkNeo4j() -> ServiceStatus:
    start = time.perf_counter()
    endpoint = _neo4jEndpoint()
    try:
        await _probeInThread(_pingNeo4j)
        return ServiceStatus(name="neo4j", status="up", latency_ms=_elapsedMs(start), endpoint=endpoint)
    except TimeoutError:
        return ServiceStatus(
            name="neo4j", status="down", latency_ms=_elapsedMs(start), endpoint=endpoint,
            detail=_timeoutDetail(),
        )
    except Exception as exc:
        return ServiceStatus(
            name="neo4j", status="down", latency_ms=_elapsedMs(start), endpoint=endpoint,
            detail=_checkFailedDetail(exc),
        )


async def _checkMilvus() -> ServiceStatus:
    start = time.perf_counter()
    endpoint = _sanitizeEndpoint(getSettings().milvusUri)
    try:
        await _probeInThread(milvus_client.checkHealth)
        return ServiceStatus(name="milvus", status="up", latency_ms=_elapsedMs(start), endpoint=endpoint)
    except TimeoutError:
        return ServiceStatus(
            name="milvus", status="down", latency_ms=_elapsedMs(start), endpoint=endpoint,
            detail=_timeoutDetail(),
        )
    except Exception as exc:
        return ServiceStatus(
            name="milvus", status="down", latency_ms=_elapsedMs(start), endpoint=endpoint,
            detail=_checkFailedDetail(exc),
        )


async def _checkEmbedding() -> ServiceStatus:
    start = time.perf_counter()
    try:
        client = await asyncio.wait_for(
            getActiveEmbeddingClient(), timeout=SERVICE_CHECK_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return ServiceStatus(
            name="embedding", status="down", latency_ms=_elapsedMs(start),
            detail=_timeoutDetail(),
        )
    except Exception as exc:
        return ServiceStatus(
            name="embedding", status="down", latency_ms=_elapsedMs(start),
            detail=_checkFailedDetail(exc),
        )
    if not client.apiBase:
        return ServiceStatus(
            name="embedding",
            status="not_configured",
            latency_ms=_elapsedMs(start),
            detail=MSG_EMBEDDING_NOT_CONFIGURED,
        )
    endpoint = _sanitizeEndpoint(client.apiBase)
    try:
        await asyncio.wait_for(client.checkHealth(), timeout=SERVICE_CHECK_TIMEOUT_SECONDS)
        return ServiceStatus(name="embedding", status="up", latency_ms=_elapsedMs(start), endpoint=endpoint)
    except TimeoutError:
        return ServiceStatus(
            name="embedding", status="down", latency_ms=_elapsedMs(start), endpoint=endpoint,
            detail=_timeoutDetail(),
        )
    except Exception as exc:
        return ServiceStatus(
            name="embedding", status="down", latency_ms=_elapsedMs(start), endpoint=endpoint,
            detail=_checkFailedDetail(exc),
        )


class ServiceStatusService:
    """核心依赖服务状态探测服务（服务状态看板数据源）。"""

    async def checkAll(self) -> ServiceStatusResponse:
        """并发探测全部核心依赖并汇总。各探测函数自带异常捕获，不会向上抛。"""
        results = await asyncio.gather(
            _checkPostgres(),
            _checkNeo4j(),
            _checkMilvus(),
            _checkEmbedding(),
        )
        return ServiceStatusResponse(
            services=list(results),
            checked_at=datetime.now(UTC),
        )
