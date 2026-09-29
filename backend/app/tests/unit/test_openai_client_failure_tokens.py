"""OpenAI 客户端失败路径的用量携带（`chat-service-assessment.md` §15 末尾第 3 项）。

背景：之前 `completeStream` 流式中断时**已测得**的 prompt/completion tokens 与
`complete_with_tools` 后处理失败时**已读取**的响应 usage 都随异常逃逸而消失
（核心约束 #3「失败路径也是计量路径」违反）。本批：

1. `LlmClientError` 新增 `tokens: tuple[int, int] | None` 字段
   （`app/domain/exceptions.py`，与 `Nl2SqlError.tokens` 同一思路）；
2. `consumedTokens()` 在 `llm_retry_policy.py` 取值优先级新增**第二档**
   `LlmClientError.tokens` > `retryGenTokens`；
3. `openai_client.completeStream` 流式中断时把累计的 `promptTokens`/`completionTokens`
   挂到 `LlmClientError.tokens`；
4. `openai_client.complete_with_tools` 后处理（tool_calls 解析）失败时把已读取的
   响应 usage 挂到 `LlmClientError.tokens`。

本文件对**真实** `OpenAiClient` 的失败路径做差分校验：测试侧用纯字典伪流 / 伪
client 独立模拟两类失败，断言 `LlmClientError.tokens` 等于期望值。

零生产数据库依赖；不调真 OpenAI SDK（mock client 即可）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self

import pytest

from app.domain.enums import ProviderType
from app.domain.exceptions import LlmClientError
from app.infrastructure.llm.openai_client import OpenAiClient


def _makeClient(client: Any, provider: ProviderType = ProviderType.OPENAI) -> OpenAiClient:
    """构造一个**注入 client** 的 `OpenAiClient`，跳过 `_buildRealClient`。"""
    fakeConfig = SimpleNamespace(model_name="test-model", api_endpoint=None)
    return OpenAiClient(fakeConfig, apiKey="sk-test", provider=provider, client=client)


class _UsageObj:
    """模拟 OpenAI 的 `CompletionUsage` 对象（`prompt_tokens` / `completion_tokens`）。

    接受 OpenAI 风格的下划线命名（`prompt_tokens=`）与驼峰（`promptTokens=`）两种调用，
    适配 SDK 与测试 fixture 不同风格。
    """

    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0, **kwargs: Any) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        if "promptTokens" in kwargs:
            self.prompt_tokens = kwargs["promptTokens"]
        if "completionTokens" in kwargs:
            self.completion_tokens = kwargs["completionTokens"]


class _ChoiceObj:
    def __init__(self, message: Any) -> None:
        self.message = message


class _FakeResponse:
    """模拟 OpenAI 的 `ChatCompletion` 响应（**非流式**）。"""

    def __init__(
        self,
        *,
        usage: _UsageObj,
        choices: list[_ChoiceObj] | None = None,
        model: str = "test-model",
    ) -> None:
        self.usage = usage
        self.choices = choices or []
        self.model = model


class _FakeChunk:
    """模拟 OpenAI 流式块（`usage` 可选：仅含 usage 的块为终块）。"""

    def __init__(
        self,
        *,
        deltaContent: str | None = None,
        usage: _UsageObj | None = None,
        choices: list[Any] | None = None,
        model: str = "test-model",
    ) -> None:
        self.usage = usage
        self.choices = choices
        self.model = model


class _StreamChunks:
    """模拟 OpenAI 的流式响应：`async for chunk in stream`。

    第一次 `__aiter__` 返回 `chunks` 序列；下一次 `__anext__` 抛 `exc`（中断）。
    """

    def __init__(self, chunks: list[_FakeChunk], exc: Exception | None = None) -> None:
        self._chunks = list(chunks)
        self._exc = exc

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> _FakeChunk:
        if self._chunks:
            return self._chunks.pop(0)
        if self._exc is not None:
            raise self._exc
        raise StopAsyncIteration


class _FakeStreamFactory:
    """模拟 `_client.chat.completions.create(..., stream=True)`。

    返回的 stream 在迭代过程中抛指定异常 —— 模拟**流式响应中途中断**。
    """

    def __init__(
        self,
        chunksBeforeError: list[_FakeChunk],
        exc: Exception,
        failOnCreate: bool = False,
    ) -> None:
        self._chunksBeforeError = chunksBeforeError
        self._exc = exc
        self._failOnCreate = failOnCreate

    def __call__(self, **kwargs: Any) -> _StreamChunks:
        if self._failOnCreate:
            raise self._exc
        # `_callWatermark` 不该出现在 kwargs（避免泄漏到 SDK）；仅看 stream 标志
        return _StreamChunks(self._chunksBeforeError, self._exc)


class _FakeClient:
    """mock 客户端：`chat.completions.create(...)` 返回 stream 或响应对象。"""

    def __init__(
        self,
        *,
        streamFactory: _FakeStreamFactory | None = None,
        response: _FakeResponse | None = None,
        createExc: Exception | None = None,
    ) -> None:
        self._streamFactory = streamFactory
        self._response = response
        self._createExc = createExc
        self.calls: list[dict] = []

        completions = self
        chat = self
        completions.chat = chat
        chat.completions = self

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._createExc is not None:
            raise self._createExc
        if self._streamFactory is not None:
            return self._streamFactory(**kwargs)
        return self._response


@pytest.fixture(autouse=True)
def _patchConcurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """`acquire_llm_concurrency` 在测试里是 no-op（避免无限等待真实信号量）。"""

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop() -> Any:
        yield None

    from app.infrastructure.llm import openai_client as oc
    monkeypatch.setattr(oc, "acquire_llm_concurrency", _noop)


# ---------------------------------------------------------------------------
# completeStream: 流式中断时把累计 tokens 挂到 LlmClientError.tokens
# ---------------------------------------------------------------------------


class TestCompleteStreamFailureTokens:
    """流式响应中断时，部分 token 数（已发送 / 已接收）必须如实携带。"""

    async def test_stream_chunk_failure_keeps_partial_tokens(self) -> None:
        """流式迭代到第 2 块时连接重置 ⇒ 已累加的 prompt_tokens=1500 / completion_tokens=200
        必须挂在 `LlmClientError.tokens`，否则审计行将少记这部分用量。

        真实 OpenAI 协议：usage 块**先于**流终止到达（不是流结束时才到），所以
        「中断时刻的部分用量」典型场景是：delta 块 + usage 块后流挂掉，usage
        已被消费但终块未发出。
        """
        chunks = [
            _FakeChunk(
                deltaContent="部分答复...",
                choices=[SimpleNamespace(delta=SimpleNamespace(content="部分答复..."))],
            ),
            # usage 终块（OpenAI 协议：放在所有 delta 之后）
            _FakeChunk(usage=_UsageObj(prompt_tokens=1500, completion_tokens=200)),
        ]
        connReset = ConnectionResetError("Connection reset by peer")
        fakeClient = _FakeClient(streamFactory=_FakeStreamFactory(chunksBeforeError=chunks, exc=connReset))
        oc_client = _makeClient(fakeClient)

        with pytest.raises(LlmClientError) as excInfo:
            async for _ in oc_client.completeStream(
                messages=[SimpleNamespace(role="user", content="问题")],
            ):
                pass

        # 关键断言：流式中断时部分 tokens 已被携带
        assert excInfo.value.tokens == (1500, 200), (
            f"流式中断时 partial tokens 必须挂上，实际 {excInfo.value.tokens}"
        )

    async def test_stream_create_failure_has_no_tokens(self) -> None:
        """`create(...)` 调用本身失败（流还没开始）⇒ 没有部分 tokens 可记。

        这是预期：流还没建立起来，没有用量可携带。`tokens` 应为 None，
        不应伪造 0（伪造 0 会被误解为"零消耗"，而实际可能是几百 token 的
        prompt 已被服务端接收但客户端没拿到 usage）。
        """
        networkExc = OSError("Name or service not known")
        fakeClient = _FakeClient(streamFactory=_FakeStreamFactory(chunksBeforeError=[], exc=networkExc, failOnCreate=True))
        oc_client = _makeClient(fakeClient)

        with pytest.raises(LlmClientError) as excInfo:
            async for _ in oc_client.completeStream(
                messages=[SimpleNamespace(role="user", content="问题")],
            ):
                pass

        assert excInfo.value.tokens is None

    async def test_stream_usage_chunk_keeps_accumulated_tokens(self) -> None:
        """流先发了若干 delta 块，再收到 usage 终块，然后又迭代一次触发异常 ⇒
        tokens 应是**终块**的值（prompt_tokens=3000, completion_tokens=500），
        而不是中断时刻的部分值。
        """
        chunks = [
            _FakeChunk(deltaContent="abc", choices=[SimpleNamespace(delta=SimpleNamespace(content="abc"))]),
            _FakeChunk(usage=_UsageObj(prompt_tokens=3000, completion_tokens=500)),
        ]
        exc = RuntimeError("stream ended unexpectedly")
        fakeClient = _FakeClient(streamFactory=_FakeStreamFactory(chunksBeforeError=chunks, exc=exc))
        oc_client = _makeClient(fakeClient)

        with pytest.raises(LlmClientError) as excInfo:
            async for _ in oc_client.completeStream(
                messages=[SimpleNamespace(role="user", content="问题")],
            ):
                pass

        # 终块 usage 已收到 → 必须挂在异常上
        assert excInfo.value.tokens == (3000, 500), (
            f"流式收到 usage 终块后异常，tokens 应是终块值，实际 {excInfo.value.tokens}"
        )


# ---------------------------------------------------------------------------
# complete_with_tools: 后处理失败时把响应 usage 挂到 LlmClientError.tokens
# ---------------------------------------------------------------------------


class TestCompleteWithToolsFailureTokens:
    """post-response 解析失败时，已读取的 usage 必须如实携带。"""

    async def test_post_response_parse_failure_carries_response_usage(self) -> None:
        """响应已收到（含 usage），但 `response.choices[0].message.tool_calls`
        解析失败（`function.arguments` 不是合法 JSON）⇒ 已读取的
        `usage.prompt_tokens=400` / `completion_tokens=80` 必须挂在异常上。
        """
        # 构造一个会触发解析失败的 tool_call：arguments 是非 JSON 字符串
        badToolCall = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="bad_tool",
                arguments="not-valid-json{{{",
            ),
        )
        response = _FakeResponse(
            usage=_UsageObj(prompt_tokens=400, completion_tokens=80),
            choices=[_ChoiceObj(message=SimpleNamespace(content="", tool_calls=[badToolCall]))],
        )
        fakeClient = _FakeClient(response=response)
        oc_client = _makeClient(fakeClient)

        with pytest.raises(LlmClientError) as excInfo:
            await oc_client.complete_with_tools(
                messages=[SimpleNamespace(role="user", content="问题")],
                tools=[{"type": "function", "function": {"name": "bad_tool"}}],
            )

        # 关键断言：响应已收到，usage 必须挂在异常上
        assert excInfo.value.tokens == (400, 80), (
            f"post-response 解析失败时 usage 必须挂上，实际 {excInfo.value.tokens}"
        )

    async def test_create_failure_has_no_tokens(self) -> None:
        """`create(...)` 本身失败（响应没收到）⇒ 没有 usage 可记，
        `tokens` 应为 None（与流式中断语义一致：不伪造）。
        """
        fakeClient = _FakeClient(createExc=ConnectionError("server unreachable"))
        oc_client = _makeClient(fakeClient)

        with pytest.raises(LlmClientError) as excInfo:
            await oc_client.complete_with_tools(
                messages=[SimpleNamespace(role="user", content="问题")],
                tools=[],
            )

        assert excInfo.value.tokens is None


# ---------------------------------------------------------------------------
# 集成：`consumedTokens()` 能从失败异常取回真实用量
# ---------------------------------------------------------------------------


class TestConsumedTokensEndToEnd:
    """`consumedTokens()` 能从 `LlmClientError.tokens` 取回真实用量 —— 集成校验。"""

    def test_consumed_tokens_reads_stream_failure_tokens(self) -> None:
        """流式中断异常的 `tokens` 能被 `consumedTokens()` 读回。"""
        from app.services.llm_retry_policy import consumedTokens

        exc = LlmClientError("流式中断", provider="qwen", detail="连接重置", tokens=(1500, 200))
        assert consumedTokens(exc) == (1500, 200)

    def test_consumed_tokens_reads_post_response_failure_tokens(self) -> None:
        """post-response 解析失败异常的 `tokens` 能被 `consumedTokens()` 读回。"""
        from app.services.llm_retry_policy import consumedTokens

        exc = LlmClientError("tool_calls 解析失败", provider="qwen", tokens=(400, 80))
        assert consumedTokens(exc) == (400, 80)

    def test_consumed_tokens_unattached_returns_zero(self) -> None:
        """无 builtin tokens、也无携带通道 ⇒ 0/0（与既有契约一致）。"""
        from app.services.llm_retry_policy import consumedTokens

        exc = LlmClientError("LLM 调用失败", provider="qwen")
        assert consumedTokens(exc) == (0, 0)