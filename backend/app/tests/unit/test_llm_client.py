"""LLM 客户端单元测试（纯逻辑，无 IO）。

TDD RED：定义期望行为。通过依赖注入 FakeOpenAi 内部客户端与 aiohttp 会话 mock Ollama HTTP。
触 DB 的 detached config 回归测试已迁至 integration/test_llm_client_detached.py
（【迁移：真实 PG】第四批）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.factory import createClient, resetFactory
from app.infrastructure.llm.ollama_client import OllamaClient
from app.infrastructure.llm.openai_client import OpenAiClient
# ===== OpenAI 内部客户端 Mock =====


class FakeChatCompletions:
    def __init__(self, responseContent: str = "hello back", usage: dict[str, int] | None = None) -> None:
        self.responseContent = responseContent
        self.usage = usage or {"prompt_tokens": 12, "completion_tokens": 8}
        self.callCount = 0
        self.lastKwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.callCount += 1
        self.lastKwargs = kwargs
        usage = self.usage

        class _Usage:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        class _Choice:
            class _Message:
                content = self.responseContent

            message = _Message()

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()
            model = kwargs.get("model", "gpt-4o")

        return _Resp()


class FakeChat:
    def __init__(self, completions: FakeChatCompletions) -> None:
        self.completions = completions


class FakeOpenAi:
    """模拟 openai.AsyncOpenAI 的最小接口。"""

    def __init__(self, completions: FakeChatCompletions | None = None) -> None:
        self.chat = FakeChat(completions or FakeChatCompletions())

    async def close(self) -> None:
        pass


class FakeLlmConfig:
    """最小 LlmConfig 替身（避免依赖 DB）。"""

    def __init__(
        self,
        *,
        modelName: str = "gpt-4o",
        provider: str = "openai",
        apiEndpoint: str | None = None,
        apiKeyEncrypted: str | None = None,
    ) -> None:
        self.model_name = modelName
        self.provider = provider
        self.api_endpoint = apiEndpoint
        self.api_key_encrypted = apiKeyEncrypted


# ===== OpenAiClient 测试 =====


class TestOpenAiClient:
    @pytest.mark.asyncio
    async def test_complete_returns_response_with_tokens(self) -> None:
        # Arrange
        fake = FakeChatCompletions(responseContent="你好", usage={"prompt_tokens": 10, "completion_tokens": 5})
        client = OpenAiClient(FakeLlmConfigModel(), apiKey="sk-test", client=FakeOpenAi(fake))
        messages = [LlmMessage(role="user", content="hi")]
        # Act
        resp = await client.complete(messages)
        # Assert
        assert resp.content == "你好"
        assert resp.promptTokens == 10
        assert resp.completionTokens == 5
        assert resp.totalTokens == 15
        assert resp.modelName == "gpt-4o"

    @pytest.mark.asyncio
    async def test_complete_passes_model_and_messages(self) -> None:
        fake = FakeChatCompletions()
        client = OpenAiClient(FakeLlmConfigModel(), apiKey="sk-test", client=FakeOpenAi(fake))
        await client.complete([LlmMessage(role="user", content="hi")], temperature=0.3, maxTokens=100)
        assert fake.callCount == 1
        assert fake.lastKwargs["model"] == "gpt-4o"
        assert fake.lastKwargs["temperature"] == 0.3
        assert fake.lastKwargs["max_tokens"] == 100
        assert fake.lastKwargs["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_complete_uses_override_model_when_provided(self) -> None:
        fake = FakeChatCompletions()
        client = OpenAiClient(FakeLlmConfigModel(), apiKey="sk-test", client=FakeOpenAi(fake))
        await client.complete([LlmMessage(role="user", content="hi")], model="gpt-4o-mini")
        assert fake.lastKwargs["model"] == "gpt-4o-mini"


def FakeLlmConfigModel(**kwargs: Any) -> FakeLlmConfig:
    return FakeLlmConfig(**kwargs)


# ===== OllamaClient 测试 =====


class _FakeOllamaResponse:
    """模拟 aiohttp 响应：status + 异步 text()/json()。"""

    def __init__(self, *, status: int = 200, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> _FakeOllamaResponse:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeOllamaSession:
    """模拟 aiohttp.ClientSession：post 返回注入的响应，并记录调用参数。"""

    def __init__(self, response: _FakeOllamaResponse) -> None:
        self._response = response
        self.postKwargs: tuple[str, dict[str, Any]] | None = None

    def post(self, url: str, **kwargs: Any) -> _FakeOllamaResponse:
        self.postKwargs = (url, kwargs)
        return self._response

    async def close(self) -> None:
        pass


class TestOllamaClient:
    @pytest.mark.asyncio
    async def test_complete_parses_ollama_response(self) -> None:
        # Arrange
        ollamaResp = {
            "model": "llama3.1",
            "message": {"role": "assistant", "content": "来自本地的回答"},
            "prompt_eval_count": 15,
            "eval_count": 7,
            "done": True,
        }
        session = _FakeOllamaSession(_FakeOllamaResponse(payload=ollamaResp))
        client = OllamaClient(
            FakeLlmConfig(modelName="llama3.1", provider="ollama"),
            baseUrl="http://localhost:11434",
            session=session,
        )
        # Act
        resp = await client.complete([LlmMessage(role="user", content="hi")])
        # Assert
        assert session.postKwargs == ("/api/chat", {"json": {"model": "llama3.1", "messages": [{"role": "user", "content": "hi"}], "stream": False}})
        assert resp.content == "来自本地的回答"
        assert resp.promptTokens == 15
        assert resp.completionTokens == 7
        assert resp.totalTokens == 22
        assert resp.modelName == "llama3.1"

    @pytest.mark.asyncio
    async def test_complete_raises_on_http_error(self) -> None:
        session = _FakeOllamaSession(_FakeOllamaResponse(status=500, text="boom"))
        client = OllamaClient(
            FakeLlmConfig(modelName="llama3.1", provider="ollama"),
            baseUrl="http://localhost:11434",
            session=session,
        )
        from app.domain.exceptions import LlmClientError

        with pytest.raises(LlmClientError):
            await client.complete([LlmMessage(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_complete_handles_missing_usage_fields(self) -> None:
        """旧版 Ollama 可能不返回 eval_count。"""
        session = _FakeOllamaSession(
            _FakeOllamaResponse(payload={"model": "llama3.1", "message": {"content": "ok"}, "done": True})
        )
        client = OllamaClient(
            FakeLlmConfig(modelName="llama3.1", provider="ollama"),
            baseUrl="http://localhost:11434",
            session=session,
        )
        resp = await client.complete([LlmMessage(role="user", content="hi")])
        assert resp.content == "ok"
        assert resp.promptTokens == 0
        assert resp.completionTokens == 0


# ===== Factory 测试 =====


class TestLlmClientFactory:
    def test_creates_openai_client_for_openai_provider(self) -> None:
        from app.config import getSettings

        resetFactory()
        config = FakeLlmConfig(modelName="gpt-4o", provider="openai", apiEndpoint="https://api.openai.com/v1")
        client = createClient(config, settings=getSettings(), apiKey="sk-test")
        assert isinstance(client, OpenAiClient)

    def test_creates_ollama_client_for_ollama_provider(self) -> None:
        from app.config import getSettings

        resetFactory()
        config = FakeLlmConfig(modelName="llama3.1", provider="ollama")
        client = createClient(config, settings=getSettings())
        assert isinstance(client, OllamaClient)

    def test_factory_caches_client_per_config_id(self) -> None:
        from app.config import getSettings

        resetFactory()
        config = FakeLlmConfig(modelName="gpt-4o", provider="openai")
        config.id = 42
        a = createClient(config, settings=getSettings(), apiKey="sk-test")
        b = createClient(config, settings=getSettings(), apiKey="sk-test")
        assert a is b
