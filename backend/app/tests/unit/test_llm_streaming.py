"""LLM 流式补全（completeStream）单元测试（纯逻辑，无 IO）。

覆盖：
- OpenAI 兼容客户端：内容逐块产出、stream/stream_options 透传、usage 最终块、异常包装
- Ollama 客户端：NDJSON 逐行解析、done 块携带 token、HTTP 异常包装
触 DB 的 detached config 回归测试已迁至 integration/test_llm_streaming_detached.py
（【迁移：真实 PG】第四批）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import ClientConnectionError

from app.domain.exceptions import LlmClientError
from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.ollama_client import OllamaClient
from app.infrastructure.llm.openai_client import OpenAiClient


def _config(model: str = "test-model") -> SimpleNamespace:
    return SimpleNamespace(model_name=model, api_endpoint=None)


# =========================================================================
# OpenAI 兼容客户端 fakes
# =========================================================================


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


# =========================================================================
# Ollama 客户端 fakes
# =========================================================================


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


# =========================================================================
# 测试
# =========================================================================


class TestOpenAiCompleteStream:
    async def test_yields_content_then_done_chunk(self) -> None:
        chunks = [_contentChunk("查"), _contentChunk("询", finish=True), _usageChunk()]
        client = OpenAiClient(_config(), apiKey="k", client=_FakeOpenAiClient(chunks))

        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")], model="m")]

        assert "".join(c.content for c in out if not c.isDone) == "查询"
        assert out[-1].isDone is True
        assert out[-1].promptTokens == 12
        assert out[-1].completionTokens == 6
        # stream + stream_options 透传
        payload = client._client.chat.completions.createKwargs
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert payload["model"] == "m"
        assert payload["messages"] == [{"role": "user", "content": "hi"}]

    async def test_wraps_sdk_error_as_llm_client_error(self) -> None:
        client = OpenAiClient(
            _config(), apiKey="k", client=_FakeOpenAiClient([], error=RuntimeError("boom"))
        )
        with pytest.raises(LlmClientError):
            async for _ in client.completeStream([LlmMessage(role="user", content="hi")]):
                pass

    async def test_returns_zero_usage_when_no_usage_chunk(self) -> None:
        """兼容代理不支持 include_usage 时，最终块 token 记为 0（近似计量）。"""
        chunks = [_contentChunk("ok", finish=True)]
        client = OpenAiClient(_config(), apiKey="k", client=_FakeOpenAiClient(chunks))

        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")])]

        assert out[-1].isDone is True
        assert out[-1].promptTokens == 0
        assert out[-1].completionTokens == 0


class TestOllamaCompleteStream:
    async def test_yields_content_and_done_with_tokens(self) -> None:
        lines = [
            '{"model":"m","message":{"role":"assistant","content":"查"},"done":false}',
            '{"model":"m","message":{"role":"assistant","content":"询"},"done":false}',
            '{"model":"m","done":true,"prompt_eval_count":10,"eval_count":5}',
        ]
        session = _FakeOllamaSession(_FakeOllamaResp(lines=lines))
        client = OllamaClient(_config(), baseUrl="http://x", session=session)

        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")], model="m")]

        assert "".join(c.content for c in out if not c.isDone) == "查询"
        assert out[-1].isDone is True
        assert out[-1].promptTokens == 10
        assert out[-1].completionTokens == 5
        # 请求体带 stream=True
        url, kwargs = session.postKwargs
        assert url == "/api/chat"
        assert kwargs["json"]["stream"] is True
        assert kwargs["json"]["model"] == "m"

    async def test_wraps_connection_error(self) -> None:
        session = _FakeOllamaSession(error=ClientConnectionError("refused"))
        client = OllamaClient(_config(), baseUrl="http://x", session=session)
        with pytest.raises(LlmClientError):
            async for _ in client.completeStream([LlmMessage(role="user", content="hi")]):
                pass

    async def test_wraps_malformed_ndjson(self) -> None:
        """非法 NDJSON（服务端异常）也须包装为 LlmClientError，保证降级契约。"""
        session = _FakeOllamaSession(_FakeOllamaResp(lines=['{"done":false}', "not-valid-json"]))
        client = OllamaClient(_config(), baseUrl="http://x", session=session)
        with pytest.raises(LlmClientError):
            async for _ in client.completeStream([LlmMessage(role="user", content="hi")]):
                pass

    async def test_skips_blank_lines_between_ndjson(self) -> None:
        """NDJSON 之间夹杂空行/空白行时须跳过，而非误报非法 JSON。"""
        session = _FakeOllamaSession(
            _FakeOllamaResp(
                lines=[
                    '{"model":"m","message":{"content":"查"},"done":false}',
                    "",
                    "   ",
                    '{"model":"m","done":true,"prompt_eval_count":2,"eval_count":1}',
                ]
            )
        )
        client = OllamaClient(_config(), baseUrl="http://x", session=session)
        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")])]
        assert "".join(c.content for c in out if not c.isDone) == "查"
        assert out[-1].isDone is True
        assert out[-1].promptTokens == 2
        assert out[-1].completionTokens == 1

    async def test_stream_ending_without_done_block_still_yields_done(self) -> None:
        """流未以 done 块结束时，仍产出零 token 的 done 块（与 OpenAI 客户端一致）。"""
        session = _FakeOllamaSession(
            _FakeOllamaResp(lines=['{"message":{"role":"assistant","content":"查"},"done":false}'])
        )
        client = OllamaClient(_config(), baseUrl="http://x", session=session)
        out = [c async for c in client.completeStream([LlmMessage(role="user", content="hi")])]
        assert out[-1].isDone is True
        assert out[-1].promptTokens == 0
        assert out[-1].completionTokens == 0
