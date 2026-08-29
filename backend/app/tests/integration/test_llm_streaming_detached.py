"""LLM completeStream detached config 回归测试（缓存复用 + 会话过期脱离）。

覆盖：缓存客户端持有首请求 ORM config，会话回滚+关闭（脱离）后调用 completeStream
不得懒加载 config.model_name（DetachedInstanceError）。
其余纯逻辑流式测试留在 unit/test_llm_streaming.py。

【迁移：真实 PG】由 unit/ 迁至 integration/（第四批），dbSession 走 integration/conftest.py
的真实 PostgreSQL + 每测试 TRUNCATE 隔离（Harness/rules/测试规范.md）；LLM 外部服务仍 mock
（_FakeOpenAiClient/_FakeOllamaSession），LlmConfig 数据层全真实。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.domain.models import LlmConfig
from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.ollama_client import OllamaClient
from app.infrastructure.llm.openai_client import OpenAiClient


def _contentChunk(text: str, finish: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text),
                finish_reason="stop" if finish else None,
            )
        ],
        usage=None,
    )


def _usageChunk(promptTokens: int = 12, completionTokens: int = 6) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=promptTokens,
            completion_tokens=completionTokens,
        ),
    )


class _FakeOpenAiChat:
    def __init__(self, chunks: list, *, error: Exception | None = None) -> None:
        self._chunks = chunks
        self._error = error
        self.createKwargs: dict | None = None

    async def create(self, **kwargs):
        self.createKwargs = kwargs
        if self._error is not None:
            raise self._error

        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


class _FakeOpenAiClient:
    """模拟 openai SDK 客户端（client.chat.completions.create 链）。"""

    def __init__(self, chunks: list, *, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=_FakeOpenAiChat(chunks, error=error))


class _FakeStreamReader:
    """模拟 aiohttp StreamReader：readline() 逐行返回字节（含换行），EOF 返回空。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        line = self._lines.pop(0)
        return (line + "\n").encode("utf-8")


class _FakeOllamaResp:
    """模拟 aiohttp 响应：status + 异步 text() + content 的 readline()。"""

    def __init__(self, *, status: int = 200, text: str = "", lines: list[str] | None = None) -> None:
        self.status = status
        self._text = text
        self.content = _FakeStreamReader(lines or [])

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> _FakeOllamaResp:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeOllamaSession:
    """模拟 aiohttp.ClientSession：post 返回注入响应或抛连接错误，并记录调用参数。"""

    def __init__(self, resp: _FakeOllamaResp | None = None, *, error: Exception | None = None) -> None:
        self._resp = resp if resp is not None else _FakeOllamaResp()
        self._error = error
        self.postKwargs: tuple[str, dict] | None = None

    def post(self, url: str, **kwargs) -> _FakeOllamaResp:
        self.postKwargs = (url, kwargs)
        if self._error is not None:
            raise self._error
        return self._resp

    async def close(self) -> None:
        pass


class TestOpenAiCompleteStreamDetached:
    """回归：缓存客户端持有首请求 ORM config，会话回滚+关闭后调用 completeStream
    不得懒加载 config.model_name（DetachedInstanceError）。"""

    @pytest.mark.asyncio
    async def test_stream_with_expired_detached_config(self, dbSession) -> None:
        # Arrange（客户端在会话存活期构造，随后回滚+关闭使 config 过期脱离）
        dbSession.add(LlmConfig(model_name="deepseek-chat", provider="openai_compatible_proxy"))
        await dbSession.commit()
        dbSession.expunge_all()
        config = (await dbSession.execute(select(LlmConfig))).scalars().one()
        chunks = [_contentChunk("查", finish=True), _usageChunk()]
        client = OpenAiClient(config, apiKey="k", client=_FakeOpenAiClient(chunks))
        await dbSession.rollback()
        await dbSession.close()
        # Act（逐块消费；before 修复在首块处抛 DetachedInstanceError）
        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")], model="m")]
        # Assert
        assert "".join(c.content for c in out if not c.isDone) == "查"
        assert out[0].modelName == "deepseek-chat"  # 快照值，而非从过期 ORM 懒加载
        assert out[-1].isDone is True


class TestOllamaCompleteStreamDetached:
    """回归（与 OpenAI 同缺陷）：Ollama 缓存客户端 config 过期脱离后 completeStream 可用快照。"""

    @pytest.mark.asyncio
    async def test_stream_with_expired_detached_config(self, dbSession) -> None:
        # Arrange（客户端在会话存活期构造，随后回滚+关闭使 config 过期脱离）
        dbSession.add(LlmConfig(model_name="deepseek-chat", provider="openai_compatible_proxy"))
        await dbSession.commit()
        dbSession.expunge_all()
        config = (await dbSession.execute(select(LlmConfig))).scalars().one()
        lines = [
            json.dumps(
                {"model": "deepseek-chat", "message": {"content": "ok"}, "done": True,
                 "prompt_eval_count": 5, "eval_count": 3}
            )
        ]
        session = _FakeOllamaSession(_FakeOllamaResp(lines=lines))
        client = OllamaClient(config, baseUrl="http://localhost:11434", session=session)
        await dbSession.rollback()
        await dbSession.close()
        # Act（不传 model → 回退快照 model_name；before 修复在首块处抛 DetachedInstanceError）
        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")])]
        # Assert
        assert "".join(c.content for c in out if not c.isDone) == "ok"
        assert out[-1].isDone is True
        assert out[-1].modelName == "deepseek-chat"
        assert out[-1].promptTokens == 5
        assert out[-1].completionTokens == 3
