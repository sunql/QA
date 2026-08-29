"""Embedding 服务单元测试（5.3 相似问答检索）。

覆盖：
- generateEmbedding：返回向量、客户端异常传播
- storeQueryEmbedding：写入 milvus、fire-and-forget 失败吞掉
- searchSimilarQueries：命中映射 SimilarQuery、无命中返回空
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import LlmClientError, MilvusError
from app.services import embedding_service as embedding_module
from app.services.embedding_service import EmbeddingService


class _FakeEmbedClient:
    """假 embedding 客户端：返回固定向量或抛出指定异常，记录调用。"""

    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.vectors = vectors if vectors is not None else [[0.1, 0.2, 0.3]]
        self.error = error
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return self.vectors


class _Item:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _SdkStyleClient:
    """模拟 openai SDK：embeddings 资源对象 + create 返回带 data 的对象（乱序 index）。"""

    def __init__(self, embeddings: list[list[float]]) -> None:
        self._embeddings = list(reversed(embeddings))  # 打乱顺序，验证按 index 排序
        self.createCalledWith: dict | None = None

    @property
    def embeddings(self):
        return _EmbedsProxy(self)


class _EmbedsProxy:
    def __init__(self, owner: _SdkStyleClient) -> None:
        self._owner = owner

    async def create(self, **kwargs) -> _Response:
        self._owner.createCalledWith = kwargs
        data = [_Item(i, vec) for i, vec in enumerate(self._owner._embeddings)]
        return _Response(data)


class _Response:
    def __init__(self, data: list[_Item]) -> None:
        self.data = data


class TestEmbeddingClient:
    async def test_embeds_and_sorts_by_index(self) -> None:
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        sdk = _SdkStyleClient([[0.1, 0.2], [0.3, 0.4]])
        client = EmbeddingClient(model="test-model", client=sdk, apiKey="k")
        vectors = await client.embed(["a", "b"])
        assert vectors == [[0.3, 0.4], [0.1, 0.2]]  # 按 index 升序恢复输入顺序
        assert sdk.createCalledWith["model"] == "test-model"
        assert sdk.createCalledWith["input"] == ["a", "b"]

    async def test_wraps_sdk_error_as_llm_client_error(self) -> None:
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        class _BoomProxy:
            async def create(self, **kwargs):
                raise RuntimeError("boom")

        class _BoomSdk:
            @property
            def embeddings(self):
                return _BoomProxy()

        client = EmbeddingClient(model="m", client=_BoomSdk(), apiKey="k")
        with pytest.raises(LlmClientError):
            await client.embed(["a"])

    async def test_raises_when_no_api_key_configured(self) -> None:
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        client = EmbeddingClient(model="m", apiKey="")  # 无 key 且无注入 client
        with pytest.raises(LlmClientError):
            await client.embed(["a"])

    def test_falls_back_to_openai_key_and_base_when_embedding_unset(self) -> None:
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        class _Settings:
            embeddingModel = "text-embedding-3-small"
            embeddingApiBase = ""
            embeddingApiKey = ""
            openaiBaseUrl = "https://api.openai.com/v1"
            openaiApiKey = "sk-openai"

        client = EmbeddingClient(settings=_Settings())
        assert client._apiKey == "sk-openai"
        assert client._apiBase == "https://api.openai.com/v1"

    def test_embedding_specific_config_takes_precedence(self) -> None:
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        class _Settings:
            embeddingModel = "text-embedding-3-small"
            embeddingApiBase = "https://embed.example.com/v1"
            embeddingApiKey = "sk-embed"
            openaiBaseUrl = "https://api.openai.com/v1"
            openaiApiKey = "sk-openai"

        client = EmbeddingClient(settings=_Settings())
        assert client._apiKey == "sk-embed"
        assert client._apiBase == "https://embed.example.com/v1"

    def test_host_override_rewrites_loopback_hostname(self) -> None:
        from app.config import Settings
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        settings = Settings(EMBEDDING_HOST_OVERRIDE="host.docker.internal")
        client = EmbeddingClient(
            model="bge-m3:latest",
            apiBase="http://localhost:11434/v1",
            apiKey="ollama",
            settings=settings,
        )
        assert client._apiBase == "http://host.docker.internal:11434/v1"

    def test_host_override_rewrites_loopback_ipv4(self) -> None:
        from app.config import Settings
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        settings = Settings(EMBEDDING_HOST_OVERRIDE="host.docker.internal")
        client = EmbeddingClient(
            model="m", apiBase="http://127.0.0.1:8080/v1", apiKey="k", settings=settings
        )
        assert client._apiBase == "http://host.docker.internal:8080/v1"

    def test_host_override_leaves_remote_url_untouched(self) -> None:
        from app.config import Settings
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        settings = Settings(EMBEDDING_HOST_OVERRIDE="host.docker.internal")
        client = EmbeddingClient(
            model="m", apiBase="https://api.example.com/v1", apiKey="k", settings=settings
        )
        assert client._apiBase == "https://api.example.com/v1"

    def test_host_override_rewrites_uppercase_loopback(self) -> None:
        from app.config import Settings
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        settings = Settings(EMBEDDING_HOST_OVERRIDE="host.docker.internal")
        client = EmbeddingClient(
            model="m", apiBase="http://LOCALHOST:11434/v1", apiKey="k", settings=settings
        )
        assert client._apiBase == "http://host.docker.internal:11434/v1"

    def test_no_host_override_is_noop(self) -> None:
        from app.infrastructure.llm.embedding_client import EmbeddingClient

        client = EmbeddingClient(model="m", apiBase="http://localhost:11434/v1", apiKey="k")
        assert client._apiBase == "http://localhost:11434/v1"


class TestGenerateEmbedding:
    async def test_returns_vector_for_text(self) -> None:
        fake = _FakeEmbedClient(vectors=[[0.1, 0.2, 0.3]])
        service = EmbeddingService(client=fake)
        vector = await service.generateEmbedding("你好")
        assert vector == [0.1, 0.2, 0.3]
        assert fake.calls == [["你好"]]

    async def test_propagates_client_error(self) -> None:
        fake = _FakeEmbedClient(error=LlmClientError("embedding 调用失败"))
        service = EmbeddingService(client=fake)
        with pytest.raises(LlmClientError):
            await service.generateEmbedding("你好")


class TestStoreQueryEmbedding:
    async def test_calls_milvus_with_generated_vector(self, monkeypatch) -> None:
        fake = _FakeEmbedClient(vectors=[[0.1, 0.2, 0.3]])
        service = EmbeddingService(client=fake)
        recorded: dict = {}

        def _insert(**kwargs) -> None:
            recorded.update(kwargs)

        monkeypatch.setattr(embedding_module.milvus, "insertQueryEmbedding", _insert)
        await service.storeQueryEmbedding(
            sessionId="s1", question="本月销量", sql="SELECT 1", datasourceId=1
        )
        assert recorded["sessionId"] == "s1"
        assert recorded["question"] == "本月销量"
        assert recorded["sql"] == "SELECT 1"
        assert recorded["embedding"] == [0.1, 0.2, 0.3]

    async def test_swallows_failures_for_fire_and_forget(self, monkeypatch) -> None:
        fake = _FakeEmbedClient(error=LlmClientError("网络错误"))
        service = EmbeddingService(client=fake)

        def _insert(**kwargs) -> None:
            raise OSError("milvus 不可用")

        monkeypatch.setattr(embedding_module.milvus, "insertQueryEmbedding", _insert)
        # fire-and-forget：可预期失败（LLM/Milvus/网络）不应向上抛，仅记录日志
        await service.storeQueryEmbedding(sessionId="s1", question="q", sql="SELECT 1")

    async def test_swallows_config_error_for_fire_and_forget(self) -> None:
        from app.domain.exceptions import ConfigError

        # resolver 维度守卫抛的 ConfigError 属 DomainError，fire-and-forget 路径应吞掉
        fake = _FakeEmbedClient(error=ConfigError("embedding 服务维度不一致"))
        service = EmbeddingService(client=fake)
        await service.storeQueryEmbedding(sessionId="s1", question="q", sql="SELECT 1")


class TestSearchSimilarQueries:
    async def test_maps_hits_to_similar_queries(self, monkeypatch) -> None:
        fake = _FakeEmbedClient(vectors=[[0.1, 0.2, 0.3]])
        service = EmbeddingService(client=fake)
        hits = [
            {"session_id": "s1", "question": "各供应商收货数量", "sql": "SELECT 1", "distance": 0.5},
        ]
        monkeypatch.setattr(embedding_module.milvus, "searchQueryEmbedding", lambda *a, **kw: hits)

        result = await service.searchSimilarQueries("收货数量", topK=3)
        assert len(result) == 1
        assert result[0].question == "各供应商收货数量"
        assert result[0].sql == "SELECT 1"
        assert 0 < result[0].similarity <= 1

    async def test_returns_empty_when_no_hits(self, monkeypatch) -> None:
        fake = _FakeEmbedClient(vectors=[[0.1, 0.2, 0.3]])
        service = EmbeddingService(client=fake)
        monkeypatch.setattr(embedding_module.milvus, "searchQueryEmbedding", lambda *a, **kw: [])

        result = await service.searchSimilarQueries("不存在的问题")
        assert result == []

    async def test_raises_milvus_error_when_search_fails(self, monkeypatch) -> None:
        from pymilvus.exceptions import MilvusException

        fake = _FakeEmbedClient(vectors=[[0.1, 0.2, 0.3]])
        service = EmbeddingService(client=fake)

        def _boom(*args, **kwargs):
            raise MilvusException(1, "server down")

        monkeypatch.setattr(embedding_module.milvus, "searchQueryEmbedding", _boom)
        with pytest.raises(MilvusError):
            await service.searchSimilarQueries("收货数量")


class TestClose:
    """资源释放（5.3 审查修复）。"""

    async def test_close_closes_memoized_client(self) -> None:
        class _ClosableFake:
            def __init__(self) -> None:
                self.closed = False

            async def embed(self, texts):
                return [[0.1, 0.2, 0.3]]

            async def close(self) -> None:
                self.closed = True

        fake = _ClosableFake()
        service = EmbeddingService(client=fake)
        await service.close()
        assert fake.closed is True

    async def test_close_noop_when_never_used(self) -> None:
        service = EmbeddingService()
        await service.close()  # 未使用过客户端，close 应为幂等 no-op
