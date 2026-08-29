"""ServiceStatusService 单元测试。

覆盖纯逻辑探测路径：monkeypatch 底层探测函数，验证 up / down / not_configured
三种状态映射与并发组装逻辑，不依赖真实外部服务。
"""

from __future__ import annotations

import asyncio
from functools import partial

from app.domain.schemas import ServiceStatus
from app.infrastructure import milvus_client
from app.services import service_status_service as svc_module
from app.services.messages_zh import (
    MSG_EMBEDDING_NOT_CONFIGURED,
    MSG_SERVICE_CHECK_FAILED,
    MSG_SERVICE_CHECK_TIMEOUT,
)
from app.services.service_status_service import ServiceStatusService


class _FakeEmbeddingClient:
    """EmbeddingClient 鸭子类型（apiBase 属性 + checkHealth）。"""

    def __init__(self, apiBase: str = "http://localhost:11434/v1", *, raiseOnCheck: bool = False) -> None:
        self.apiBase = apiBase
        self._raiseOnCheck = raiseOnCheck

    async def checkHealth(self) -> None:
        if self._raiseOnCheck:
            raise RuntimeError("connection refused")


def _up(name: str) -> ServiceStatus:
    return ServiceStatus(name=name, status="up", latency_ms=1, endpoint="endpoint://x")


# ===== checkAll 组装 =====


async def test_checkAll_returns_four_services_in_order(monkeypatch) -> None:
    async def _stub(name: str) -> ServiceStatus:
        return _up(name)

    monkeypatch.setattr(svc_module, "_checkPostgres", partial(_stub, "postgresql"))
    monkeypatch.setattr(svc_module, "_checkNeo4j", partial(_stub, "neo4j"))
    monkeypatch.setattr(svc_module, "_checkMilvus", partial(_stub, "milvus"))
    monkeypatch.setattr(svc_module, "_checkEmbedding", partial(_stub, "embedding"))

    response = await ServiceStatusService().checkAll()

    assert [s.name for s in response.services] == ["postgresql", "neo4j", "milvus", "embedding"]
    assert all(s.status == "up" for s in response.services)
    assert response.checked_at is not None


# ===== PostgreSQL =====


async def test_postgres_up(monkeypatch) -> None:
    async def _ping() -> None:
        return None

    monkeypatch.setattr(svc_module, "_pingPostgres", _ping)
    monkeypatch.setattr(
        svc_module, "_postgresEndpoint", lambda: "postgresql+asyncpg://qa_user@localhost:5432/qa_metadata"
    )

    status = await svc_module._checkPostgres()

    assert status.name == "postgresql"
    assert status.status == "up"
    assert status.endpoint == "postgresql+asyncpg://qa_user@localhost:5432/qa_metadata"


async def test_postgres_down_on_connection_error(monkeypatch) -> None:
    async def _ping() -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(svc_module, "_pingPostgres", _ping)
    monkeypatch.setattr(svc_module, "_postgresEndpoint", lambda: "postgresql+asyncpg://qa_user@h/db")

    status = await svc_module._checkPostgres()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_FAILED


async def test_postgres_timeout(monkeypatch) -> None:
    async def _ping() -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(svc_module, "_pingPostgres", _ping)
    monkeypatch.setattr(svc_module, "_postgresEndpoint", lambda: "postgresql+asyncpg://qa_user@h/db")
    monkeypatch.setattr(svc_module, "SERVICE_CHECK_TIMEOUT_SECONDS", 0.01)

    status = await svc_module._checkPostgres()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_TIMEOUT


# ===== Neo4j =====


async def test_neo4j_up(monkeypatch) -> None:
    monkeypatch.setattr(svc_module, "_pingNeo4j", lambda: None)
    monkeypatch.setattr(svc_module, "_sanitizeUri", lambda uri: "bolt://localhost:7687")

    status = await svc_module._checkNeo4j()

    assert status.name == "neo4j"
    assert status.status == "up"
    assert status.endpoint == "bolt://localhost:7687"


async def test_neo4j_down(monkeypatch) -> None:
    def _boom() -> None:
        raise ConnectionError("neo4j unreachable")

    monkeypatch.setattr(svc_module, "_pingNeo4j", _boom)
    monkeypatch.setattr(svc_module, "_sanitizeUri", lambda uri: "bolt://localhost:7687")

    status = await svc_module._checkNeo4j()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_FAILED


# ===== Milvus =====


async def test_milvus_up(monkeypatch) -> None:
    monkeypatch.setattr(milvus_client, "checkHealth", lambda: None)

    status = await svc_module._checkMilvus()

    assert status.name == "milvus"
    assert status.status == "up"
    assert status.endpoint == "http://localhost:19530"


async def test_milvus_down(monkeypatch) -> None:
    def _boom() -> None:
        raise ConnectionError("milvus unreachable")

    monkeypatch.setattr(milvus_client, "checkHealth", _boom)

    status = await svc_module._checkMilvus()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_FAILED


# ===== Embedding =====


async def _resolve(client: _FakeEmbeddingClient) -> _FakeEmbeddingClient:
    return client


async def test_embedding_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(svc_module, "getActiveEmbeddingClient", partial(_resolve, _FakeEmbeddingClient(apiBase="")))

    status = await svc_module._checkEmbedding()

    assert status.name == "embedding"
    assert status.status == "not_configured"
    assert status.detail == MSG_EMBEDDING_NOT_CONFIGURED


async def test_embedding_up(monkeypatch) -> None:
    client = _FakeEmbeddingClient(apiBase="http://localhost:11434/v1")
    monkeypatch.setattr(svc_module, "getActiveEmbeddingClient", partial(_resolve, client))

    status = await svc_module._checkEmbedding()

    assert status.status == "up"
    assert status.endpoint == "http://localhost:11434/v1"


async def test_embedding_down_on_probe_error(monkeypatch) -> None:
    client = _FakeEmbeddingClient(raiseOnCheck=True)
    monkeypatch.setattr(svc_module, "getActiveEmbeddingClient", partial(_resolve, client))

    status = await svc_module._checkEmbedding()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_FAILED


async def test_embedding_down_on_resolve_error(monkeypatch) -> None:
    async def _boom() -> _FakeEmbeddingClient:
        raise RuntimeError("db down")

    monkeypatch.setattr(svc_module, "getActiveEmbeddingClient", _boom)

    status = await svc_module._checkEmbedding()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_FAILED


async def test_embedding_resolve_timeout(monkeypatch) -> None:
    async def _slow() -> _FakeEmbeddingClient:
        await asyncio.sleep(1)
        return _FakeEmbeddingClient()

    monkeypatch.setattr(svc_module, "getActiveEmbeddingClient", _slow)
    monkeypatch.setattr(svc_module, "SERVICE_CHECK_TIMEOUT_SECONDS", 0.01)

    status = await svc_module._checkEmbedding()

    assert status.status == "down"
    assert status.detail == MSG_SERVICE_CHECK_TIMEOUT


# ===== _sanitizeEndpoint 脱敏 =====


def test_sanitize_endpoint_strips_schemed_userinfo() -> None:
    assert (
        svc_module._sanitizeEndpoint("http://user:pass@host:19530/v1")
        == "http://host:19530/v1"
    )


def test_sanitize_endpoint_strips_schemeless_userinfo() -> None:
    assert svc_module._sanitizeEndpoint("user:pass@host:19530") == "host:19530"


def test_sanitize_endpoint_strips_query_and_fragment() -> None:
    assert (
        svc_module._sanitizeEndpoint("http://host:11434/v1?api_key=secret#frag")
        == "http://host:11434/v1"
    )


def test_sanitize_endpoint_keeps_plain_endpoint() -> None:
    assert svc_module._sanitizeEndpoint("http://localhost:19530") == "http://localhost:19530"


async def test_milvus_down_endpoint_does_not_leak_credentials(monkeypatch) -> None:
    def _boom() -> None:
        raise ConnectionError("milvus unreachable")

    from types import SimpleNamespace

    monkeypatch.setattr(milvus_client, "checkHealth", _boom)
    monkeypatch.setattr(
        svc_module, "getSettings",
        lambda: SimpleNamespace(milvusUri="milvus://user:pass@milvus-host:19530"),
    )

    status = await svc_module._checkMilvus()

    assert status.status == "down"
    assert status.endpoint == "milvus://milvus-host:19530"
    assert status.detail == MSG_SERVICE_CHECK_FAILED
