"""Embedding 服务 resolver 单元测试。

monkeypatch getSessionFactory 返回 fake session，覆盖：
- 无激活 provider → 回退环境变量客户端
- 有激活 provider → 按 base_url/model/key 构建客户端
- 缓存命中不重复查询 DB
- invalidate 后重新解析
- 维度不一致 → 抛 ConfigError
- provider_type 未知 → 抛 ConfigError（不再静默当 OpenAI 兼容）
- env 回退路径的维度守卫（H7）
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

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
        providerType: str = "ollama",
    ) -> None:
        self.id = providerId
        self.name = name
        self.base_url = baseUrl
        self.model_name = modelName
        self.api_key_encrypted = apiKeyEncrypted
        self.dimension = dimension
        self.provider_type = providerType


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


class TestProviderTypeGuard:
    """H7：`provider_type` 不再是不读的死元数据。

    已知集合与前端 `types/embeddingProvider.ts::EmbeddingProviderType` 一致
    （`ollama | omlx | openai_compatible`）。未知值必须 fail-fast，而不是静默按
    OpenAI 兼容处理 —— 后者会在客户端形态其实不兼容时表现为难以定位的调用错误。
    客户端构造目前**只**实现 OpenAI 兼容形态（见模块 docstring），故已知值之间不做
    分支 —— 本批不发明无法验证的协议。
    """

    @pytest.mark.parametrize("providerType", ["ollama", "omlx", "openai_compatible"])
    async def test_known_types_are_accepted(self, monkeypatch, providerType: str) -> None:
        """三个已知值都能解析（prod 现有 ollama / omlx 行必须继续可用）。"""
        _patchProvider(monkeypatch, _FakeProvider(providerType=providerType))
        client = await factory_module.getActiveEmbeddingClient()
        assert client._model == "bge-m3:latest"

    async def test_unknown_type_fails_fast(self, monkeypatch) -> None:
        provider = _FakeProvider(providerType="azure_openai", name="Azure 不兼容端点")
        _patchProvider(monkeypatch, provider)
        with pytest.raises(ConfigError) as excinfo:
            await factory_module.getActiveEmbeddingClient()
        message = str(excinfo.value)
        # 可操作：说出被拒的具体值 + 已知集合，而不是笼统「类型不合法」
        assert "azure_openai" in message
        assert "ollama" in message and "openai_compatible" in message

    async def test_empty_type_fails_fast(self, monkeypatch) -> None:
        """空值也算未知（列 nullable=False 但历史/直写可能留空），不得当作默认放行。"""
        _patchProvider(monkeypatch, _FakeProvider(providerType=""))
        with pytest.raises(ConfigError):
            await factory_module.getActiveEmbeddingClient()

    async def test_type_guard_runs_before_dimension_guard(self, monkeypatch) -> None:
        """先校验类型再校验维度：两个都错时报**类型**（更根本的配置错误）。"""
        _patchProvider(
            monkeypatch,
            _FakeProvider(providerType="bogus", dimension=1536),
        )
        with pytest.raises(ConfigError) as excinfo:
            await factory_module.getActiveEmbeddingClient()
        assert "bogus" in str(excinfo.value)


def _patchSettings(monkeypatch, **overrides) -> None:
    """替换 factory 模块内的 getSettings（只影响 factory 自己的读取）。

    `EmbeddingClient()` 无参构造会读它自己模块的 getSettings —— 用真实 Settings，
    故这里只需提供 factory 读到的字段。
    """
    defaults = {"embeddingModel": "bge-m3:latest", "embeddingDimension": None}
    monkeypatch.setattr(
        factory_module, "getSettings", lambda: SimpleNamespace(**{**defaults, **overrides})
    )


class TestEnvFallbackDimensionGuard:
    """H7：维度守卫必须覆盖 env 回退路径（此前 `return` 早于守卫 = 静默腐化）。"""

    async def test_declared_dimension_mismatch_fails_fast(self, monkeypatch) -> None:
        """env 声明维度与 Milvus 集合不一致 → fail-fast（与 registry 路径同口径）。"""
        _patchProvider(monkeypatch, None)
        _patchSettings(monkeypatch, embeddingDimension=768)
        with pytest.raises(ConfigError) as excinfo:
            await factory_module.getActiveEmbeddingClient()
        message = str(excinfo.value)
        assert "768" in message and "1024" in message
        # 说清是哪条路径出问题（env 与 registry 的处置动作不同）
        assert "env" in message

    async def test_declared_dimension_match_builds_client(self, monkeypatch) -> None:
        _patchProvider(monkeypatch, None)
        _patchSettings(monkeypatch, embeddingDimension=1024)
        client = await factory_module.getActiveEmbeddingClient()
        assert isinstance(client, EmbeddingClient)

    async def test_undeclared_dimension_warns_instead_of_silence(
        self, monkeypatch, caplog
    ) -> None:
        """未声明维度时**不能静默**：模型→维度无可靠映射，故不猜（猜错会拦正确部署），
        但必须把「这条路没守卫」显式说出来。"""
        _patchProvider(monkeypatch, None)
        _patchSettings(monkeypatch, embeddingDimension=None)
        with caplog.at_level(logging.WARNING, logger=factory_module.__name__):
            client = await factory_module.getActiveEmbeddingClient()
        assert isinstance(client, EmbeddingClient)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("EMBEDDING_DIMENSION" in m for m in warnings), warnings

    async def test_registry_path_guard_is_unaffected(self, monkeypatch) -> None:
        """有激活 provider 时不看 env 声明（registry 的 dimension 列才是 SSOT）。"""
        _patchProvider(monkeypatch, _FakeProvider(dimension=1024))
        _patchSettings(monkeypatch, embeddingDimension=768)
        client = await factory_module.getActiveEmbeddingClient()
        assert client._model == "bge-m3:latest"
