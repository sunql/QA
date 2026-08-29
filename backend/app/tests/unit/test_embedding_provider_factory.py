"""Embedding 服务 resolver 单元测试。

monkeypatch getSessionFactory 返回 fake session，覆盖：
- 无激活 provider → 回退环境变量客户端
- 有激活 provider → 按 base_url/model/key 构建客户端
- 缓存命中不重复查询 DB
- invalidate 后重新解析
- 维度不一致 → 抛 ConfigError
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ConfigError
from app.infrastructure.llm import embedding_provider_factory as factory_module
from app.infrastructure.llm.embedding_client import EmbeddingClient
from app.infrastructure.security.crypto import encryptApiKey


class _FakeProvider:
    """激活 provider 快照鸭子类型。"""

    def __init__(
        self,
        *,
        providerId: int = 1,
        name: str = "Ollama bge-m3",
        baseUrl: str = "http://localhost:11434/v1",
        modelName: str = "bge-m3:latest",
        apiKeyEncrypted: str | None = None,
        dimension: int = 1024,
    ) -> None:
        self.id = providerId
        self.name = name
        self.base_url = baseUrl
        self.model_name = modelName
        self.api_key_encrypted = apiKeyEncrypted
        self.dimension = dimension


class _FakeSession:
    """记录 execute 调用次数，返回固定 provider（None 表示无激活）；error 非空则抛出。"""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.executeCalls = 0
        self.error: Exception | None = None

    async def execute(self, stmt):
        self.executeCalls += 1
        if self.error is not None:
            raise self.error
        return _Result(self.provider)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Result:
    def __init__(self, provider) -> None:
        self._provider = provider

    def scalars(self) -> "_Result":
        return self

    def first(self):
        return self._provider


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


@pytest.fixture(autouse=True)
def _resetCache() -> None:
    factory_module.resetEmbeddingClientCache()
    yield
    factory_module.resetEmbeddingClientCache()


def _patchProvider(monkeypatch: pytest.MonkeyPatch, provider) -> _FakeSession:
    session = _FakeSession(provider)
    monkeypatch.setattr(
        factory_module, "getSessionFactory", lambda: _FakeFactory(session)
    )
    return session


class TestGetActiveEmbeddingClient:
    async def test_falls_back_to_env_when_no_active_provider(self, monkeypatch) -> None:
        # Arrange: 无激活 provider
        _patchProvider(monkeypatch, None)
        # Act
        client = await factory_module.getActiveEmbeddingClient()
        # Assert: 返回环境变量客户端
        assert isinstance(client, EmbeddingClient)

    async def test_builds_client_from_active_provider(self, monkeypatch) -> None:
        # Arrange: 激活 provider，key 为加密后的 "ollama"
        encrypted = encryptApiKey("ollama")
        provider = _FakeProvider(apiKeyEncrypted=encrypted)
        _patchProvider(monkeypatch, provider)
        # Act
        client = await factory_module.getActiveEmbeddingClient()
        # Assert
        assert isinstance(client, EmbeddingClient)
        assert client._model == "bge-m3:latest"
        assert client._apiBase == "http://localhost:11434/v1"
        assert client._apiKey == "ollama"

    async def test_builds_client_with_empty_key_for_local_models(self, monkeypatch) -> None:
        # Arrange: 无 key 的本地服务（NULL api_key_encrypted）
        _patchProvider(monkeypatch, _FakeProvider(apiKeyEncrypted=None))
        # Act
        client = await factory_module.getActiveEmbeddingClient()
        # Assert
        assert client._apiKey == ""

    async def test_cache_hit_skips_db_query(self, monkeypatch) -> None:
        # Arrange
        session = _patchProvider(monkeypatch, _FakeProvider())
        # Act
        first = await factory_module.getActiveEmbeddingClient()
        second = await factory_module.getActiveEmbeddingClient()
        # Assert: 同一实例，DB 只查一次
        assert first is second
        assert session.executeCalls == 1

    async def test_invalidate_re_resolves(self, monkeypatch) -> None:
        # Arrange
        session = _patchProvider(monkeypatch, _FakeProvider(providerId=1))
        await factory_module.getActiveEmbeddingClient()
        # Act: 变更 provider 后失效
        session.provider = _FakeProvider(
            providerId=2, name="oMLX bge-m3 FP16", modelName="bge-m3-mlx-fp16"
        )
        await factory_module.invalidateEmbeddingClientCache()
        client = await factory_module.getActiveEmbeddingClient()
        # Assert
        assert session.executeCalls == 2
        assert client._model == "bge-m3-mlx-fp16"

    async def test_dimension_mismatch_raises_config_error(self, monkeypatch) -> None:
        # Arrange: 1536 维与 Milvus 集合 1024 维不一致
        _patchProvider(monkeypatch, _FakeProvider(dimension=1536))
        # Act / Assert
        with pytest.raises(ConfigError) as excinfo:
            await factory_module.getActiveEmbeddingClient()
        assert "输出维度 1536 与 Milvus 集合维度 1024 不一致" in str(excinfo.value)


class TestDbErrorClassification:
    """resolver 对 DB 错误分级：连接性错误回退环境变量，配置性错误 fail-fast。"""

    async def test_operational_error_falls_back_to_env(self, monkeypatch) -> None:
        from sqlalchemy.exc import OperationalError

        session = _FakeSession(None)
        session.error = OperationalError("SELECT", {}, Exception("connection refused"))
        monkeypatch.setattr(factory_module, "getSessionFactory", lambda: _FakeFactory(session))
        # Act: 连接抖动 → 回退环境变量，不抛错
        client = await factory_module.getActiveEmbeddingClient()
        assert isinstance(client, EmbeddingClient)
        assert session.executeCalls == 1

    async def test_programming_error_fails_fast(self, monkeypatch) -> None:
        from sqlalchemy.exc import ProgrammingError

        session = _FakeSession(None)
        session.error = ProgrammingError(
            "SELECT", {}, Exception("relation 'embedding_provider' does not exist")
        )
        monkeypatch.setattr(factory_module, "getSessionFactory", lambda: _FakeFactory(session))
        # Act / Assert: 缺表等配置性错误向上抛，不静默用错模型
        with pytest.raises(ProgrammingError):
            await factory_module.getActiveEmbeddingClient()
