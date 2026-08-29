"""LLM 客户端 detached config 回归测试（缓存复用 + 会话过期脱离）。

覆盖：缓存客户端持有首请求 ORM config，会话回滚+关闭（脱离）后调用 complete
不得懒加载 config.model_name（DetachedInstanceError）。
其余纯逻辑客户端/工厂测试留在 unit/test_llm_client.py。

【迁移：真实 PG】由 unit/ 迁至 integration/（第四批），dbSession 走 integration/conftest.py
的真实 PostgreSQL + 每测试 TRUNCATE 隔离（Harness/rules/测试规范.md）；LLM 外部服务仍 mock
（FakeOpenAi/_FakeOllamaSession），LlmConfig 数据层全真实。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.domain.models import LlmConfig
from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.ollama_client import OllamaClient
from app.infrastructure.llm.openai_client import OpenAiClient


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


class TestClientWithDetachedConfig:
    """createClient 按 config_id 缓存单例客户端；缓存客户端持有首请求的 ORM config。

    与线上一致的时序：客户端在 config 会话仍存活时创建（快照标量）；随后该请求以
    回滚结束（getDb 的 except: rollback 会过期全部属性）且会话关闭（脱离）。缓存客户端
    在后续请求被复用并调用 complete/completeStream——若调用期懒加载 config.model_name
    会抛 DetachedInstanceError（被包装为通用"服务内部错误"）。回归目标：客户端在
    __init__ 快照标量，调用期不再触碰 ORM。
    """

    @staticmethod
    async def _makeClientThenDetach(dbSession: Any, makeClient: Any) -> Any:
        """SELECT 加载 config → 在会话存活期构造客户端（快照）→ 回滚+关闭使 config 过期脱离。"""
        dbSession.add(LlmConfig(model_name="deepseek-chat", provider="openai_compatible_proxy"))
        await dbSession.commit()
        dbSession.expunge_all()
        loaded = (await dbSession.execute(select(LlmConfig))).scalars().one()
        client = makeClient(loaded)
        await dbSession.rollback()
        await dbSession.close()
        return client

    @pytest.mark.asyncio
    async def test_openai_complete_with_detached_config(self, dbSession: Any) -> None:
        # Arrange
        fake = FakeChatCompletions(responseContent="ok", usage={"prompt_tokens": 1, "completion_tokens": 1})
        client = await self._makeClientThenDetach(
            dbSession, lambda c: OpenAiClient(c, apiKey="sk-test", client=FakeOpenAi(fake))
        )
        # Act（model 传入，走 generateQueryPlan 的调用形态；line 98 仍会取 config.model_name）
        resp = await client.complete([LlmMessage(role="user", content="hi")], model="deepseek-chat")
        # Assert
        assert resp.content == "ok"
        assert resp.modelName == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_ollama_complete_with_detached_config(self, dbSession: Any) -> None:
        # Arrange
        session = _FakeOllamaSession(
            _FakeOllamaResponse(
                payload={"model": "deepseek-chat", "message": {"role": "assistant", "content": "ok"}, "done": True}
            )
        )
        client = await self._makeClientThenDetach(
            dbSession, lambda c: OllamaClient(c, baseUrl="http://localhost:11434", session=session)
        )
        # Act（不传 model → 需回退 config.model_name）
        resp = await client.complete([LlmMessage(role="user", content="hi")])
        # Assert
        assert resp.content == "ok"
        assert resp.modelName == "deepseek-chat"
