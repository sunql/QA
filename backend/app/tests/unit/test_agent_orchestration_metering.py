"""Task 6 unit tests: metered LLM wrapper, fixed-model factory, cost summary.

No database and no provider are involved: the recorder is faked so the tests
pin the *accounting contract* of the wrapper (which usage row is written, with
which tokens / cost source / call status) and the factory contract (the same
config object is always wrapped, never a fallback model).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from app.domain.exceptions import LlmClientError, LLMUnavailableError
from app.infrastructure.llm.base_client import (
    LlmMessage,
    LlmResponse,
    LlmResponseWithTools,
    StreamChunk,
    ToolCall,
)
from app.services import agent_orchestration_metering as meteringModule
from app.services.agent_orchestration_metering import (
    MeteredLlmClient,
    MeteredLlmFactory,
    summarizeCostSources,
)
from app.services.chat_usage import UsageMixin

# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeRecorder:
    """Captures every accounting call the wrapper makes."""

    def __init__(self) -> None:
        self.modelConfigId = 42
        self.modelName = "test-model"
        self.usageCalls: list[dict[str, Any]] = []
        self.invalidCalls: list[int] = []
        self.costCalls: list[tuple[int, int, int | None]] = []

    async def calculateCost(
        self, promptTokens: int, completionTokens: int, cachedTokens: int | None = None
    ) -> Decimal:
        self.costCalls.append((promptTokens, completionTokens, cachedTokens))
        return Decimal("0.5")

    async def recordUsage(self, **kwargs: Any) -> int:
        self.usageCalls.append(kwargs)
        return 1000 + len(self.usageCalls)

    async def markOutputInvalid(self, usageId: int) -> None:
        self.invalidCalls.append(usageId)

    @property
    def lastUsage(self) -> dict[str, Any]:
        assert self.usageCalls, "no usage row was recorded"
        return self.usageCalls[-1]


class _FakeLlmClient:
    """A provider double that never talks to the network."""

    def __init__(
        self,
        response: LlmResponse | None = None,
        error: Exception | None = None,
        toolsResponse: LlmResponseWithTools | None = None,
        chunks: list[StreamChunk] | None = None,
        streamError: Exception | None = None,
    ) -> None:
        self.response = response or LlmResponse(
            content="ok",
            modelName="test-model",
            promptTokens=11,
            completionTokens=7,
            totalTokens=18,
            cachedTokens=3,
        )
        self.error = error
        self.toolsResponse = toolsResponse
        self.chunks = chunks if chunks is not None else [
            StreamChunk(content="a", isDone=False, promptTokens=0, completionTokens=0, modelName="test-model"),
            StreamChunk(
                content="",
                isDone=True,
                promptTokens=21,
                completionTokens=9,
                modelName="test-model",
                cachedTokens=5,
            ),
        ]
        self.streamError = streamError
        self.completeCalls: list[dict[str, Any]] = []
        self.toolsCalls: list[dict[str, Any]] = []
        self.closed = False

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        self.completeCalls.append({"messages": messages, "model": model})
        if self.error is not None:
            raise self.error
        return self.response

    async def complete_with_tools(
        self,
        messages: list[LlmMessage],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
    ) -> LlmResponseWithTools:
        self.toolsCalls.append({"messages": messages, "tools": tools})
        if self.error is not None:
            raise self.error
        assert self.toolsResponse is not None
        return self.toolsResponse

    async def completeStream(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        for chunk in self.chunks:
            yield chunk
        if self.streamError is not None:
            raise self.streamError

    async def close(self) -> None:
        self.closed = True


def _client(
    fake: _FakeLlmClient, recorder: _FakeRecorder
) -> MeteredLlmClient:
    return MeteredLlmClient(
        fake, recorder, runId=7, stepId=9, callerType="PLANNER"
    )


# ---------------------------------------------------------------------------
# cost source summary
# ---------------------------------------------------------------------------


def test_cost_source_and_completeness_rules() -> None:
    assert summarizeCostSources([]) == ("UNKNOWN", False)
    assert summarizeCostSources(["MEASURED", "MEASURED"]) == ("MEASURED", True)
    assert summarizeCostSources(["ESTIMATED", "ESTIMATED"]) == ("ESTIMATED", True)
    assert summarizeCostSources(["MEASURED", "ESTIMATED"]) == ("MIXED", True)
    assert summarizeCostSources(["MEASURED", "UNKNOWN"]) == ("UNKNOWN", False)


def test_cost_source_summary_ignores_duplicates_and_order() -> None:
    assert summarizeCostSources(["ESTIMATED", "MEASURED", "ESTIMATED"]) == (
        "MIXED",
        True,
    )
    assert summarizeCostSources(["UNKNOWN", "ESTIMATED"]) == ("UNKNOWN", False)


def test_cost_source_summary_treats_unrecognized_source_as_unknown() -> None:
    assert summarizeCostSources(["SOMETHING_ELSE"]) == ("UNKNOWN", False)


# ---------------------------------------------------------------------------
# successful calls
# ---------------------------------------------------------------------------


async def test_complete_records_success_with_estimated_cost() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    response = await _client(fake, recorder).complete([LlmMessage(role="user", content="hi")])

    assert response.content == "ok"
    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "ESTIMATED"
    assert usage["promptTokens"] == 11
    assert usage["completionTokens"] == 7
    assert usage["cachedTokens"] == 3
    assert usage["cost"] == Decimal("0.5")
    assert usage["runId"] == 7
    assert usage["stepId"] == 9
    assert usage["callerType"] == "PLANNER"
    assert usage["modelConfigId"] == 42
    assert usage["modelName"] == "test-model"


async def test_complete_does_not_request_a_fallback_model() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    await _client(fake, recorder).complete([LlmMessage(role="user", content="hi")])

    assert len(fake.completeCalls) == 1
    assert len(recorder.usageCalls) == 1


async def test_complete_for_planning_returns_metered_completion() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    completion = await _client(fake, recorder).completeForPlanning(
        [LlmMessage(role="user", content="hi")]
    )

    assert completion.usageId == 1001
    assert completion.response.content == "ok"
    assert recorder.lastUsage["callStatus"] == "SUCCEEDED"


async def test_mark_output_invalid_only_touches_the_call_status() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    client = _client(fake, recorder)

    await client.markOutputInvalid(4242)

    assert recorder.invalidCalls == [4242]
    # The invalidation must not create a second usage row.
    assert recorder.usageCalls == []


async def test_complete_with_tools_maps_provider_usage() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(
        toolsResponse=LlmResponseWithTools(
            content="done",
            tool_calls=[ToolCall(id="t1", name="x", args={})],
            usage={
                "prompt_tokens": 31,
                "completion_tokens": 13,
                "total_tokens": 44,
            },
            model="test-model",
        )
    )
    result = await _client(fake, recorder).complete_with_tools(
        [LlmMessage(role="user", content="hi")]
    )

    assert result.content == "done"
    usage = recorder.lastUsage
    assert usage["promptTokens"] == 31
    assert usage["completionTokens"] == 13
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "ESTIMATED"


async def test_complete_with_tools_without_usage_is_recorded_as_unknown() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(
        toolsResponse=LlmResponseWithTools(content="done", usage=None)
    )
    await _client(fake, recorder).complete_with_tools(
        [LlmMessage(role="user", content="hi")]
    )

    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------


async def test_transport_failure_without_usage_is_recorded_as_unknown() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(error=LlmClientError("provider down"))
    client = _client(fake, recorder)

    with pytest.raises(LlmClientError):
        await client.complete([LlmMessage(role="user", content="hi")])

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0


async def test_failure_with_carried_tokens_keeps_the_measured_usage() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(error=LlmClientError("boom", tokens=(12, 5)))
    client = _client(fake, recorder)

    with pytest.raises(LlmClientError):
        await client.complete([LlmMessage(role="user", content="hi")])

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "ESTIMATED"
    assert usage["promptTokens"] == 12
    assert usage["completionTokens"] == 5


async def test_unexpected_error_is_recorded_as_unknown_and_reraised() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(error=RuntimeError("kaboom"))
    client = _client(fake, recorder)

    with pytest.raises(RuntimeError):
        await client.complete([LlmMessage(role="user", content="hi")])

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"


async def test_failed_call_does_not_ask_the_provider_again() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(error=LlmClientError("provider down"))
    client = _client(fake, recorder)

    with pytest.raises(LlmClientError):
        await client.complete([LlmMessage(role="user", content="hi")])

    assert len(fake.completeCalls) == 1
    assert len(recorder.usageCalls) == 1


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_complete_stream_yields_chunks_unchanged_and_records_terminal_usage() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    chunks = [
        chunk async for chunk in _client(fake, recorder).completeStream(
            [LlmMessage(role="user", content="hi")]
        )
    ]

    assert [chunk.content for chunk in chunks] == ["a", ""]
    assert chunks[-1].isDone is True
    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["promptTokens"] == 21
    assert usage["completionTokens"] == 9
    assert usage["cachedTokens"] == 5


async def test_complete_stream_failure_before_terminal_usage_is_unknown() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(
        chunks=[StreamChunk(content="a", isDone=False, promptTokens=0, completionTokens=0, modelName="m")],
        streamError=LlmClientError("stream broke"),
    )
    client = _client(fake, recorder)

    seen = []
    with pytest.raises(LlmClientError):
        async for chunk in client.completeStream([LlmMessage(role="user", content="hi")]):
            seen.append(chunk)

    assert [chunk.content for chunk in seen] == ["a"]
    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"


async def test_close_delegates_and_writes_no_usage() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    await _client(fake, recorder).close()

    assert fake.closed is True
    assert recorder.usageCalls == []


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


class _StubConfig:
    def __init__(self) -> None:
        self.id = 5
        self.model_name = "stub-model"


def test_factory_always_wraps_the_same_config_object(monkeypatch: Any) -> None:
    seen: list[Any] = []
    fake = _FakeLlmClient()

    def _fakeCreateClient(config: Any, **kwargs: Any) -> Any:
        seen.append(config)
        return fake

    monkeypatch.setattr(meteringModule, "createClient", _fakeCreateClient)
    config = _StubConfig()
    factory = MeteredLlmFactory(None, 7, 9, "PLANNER", config)

    first = factory("ignored-argument")
    second = factory("another-argument")

    assert seen == [config, config]
    assert seen[0] is config
    assert isinstance(first, MeteredLlmClient)
    assert isinstance(second, MeteredLlmClient)
    assert first is not second


async def test_factory_without_provider_raises_unavailable_and_records_failure(
    monkeypatch: Any,
) -> None:
    recorder = _FakeRecorder()
    monkeypatch.setattr(meteringModule, "createClient", lambda config, **kwargs: None)
    monkeypatch.setattr(
        meteringModule, "OrchestrationUsageRecorder", lambda *a, **k: recorder
    )
    factory = MeteredLlmFactory(None, 7, 9, "PLANNER", _StubConfig())

    client = factory("ignored")
    with pytest.raises(LLMUnavailableError):
        await client.complete([LlmMessage(role="user", content="hi")])

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0


async def test_factory_fail_fast_variant_raises_unavailable_immediately(
    monkeypatch: Any,
) -> None:
    recorder = _FakeRecorder()
    monkeypatch.setattr(meteringModule, "createClient", lambda config, **kwargs: None)
    monkeypatch.setattr(
        meteringModule, "OrchestrationUsageRecorder", lambda *a, **k: recorder
    )
    factory = MeteredLlmFactory(None, 7, 9, "PLANNER", _StubConfig())

    with pytest.raises(LLMUnavailableError):
        await factory.createMeteredClient("ignored")

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# cost delegate
# ---------------------------------------------------------------------------


def test_calculate_cost_delegate_matches_the_established_cost_math() -> None:
    class _Cfg:
        cost_per_1k_input = 0.0014
        cost_per_1k_output = 0.0028

    config = _Cfg()
    expected = UsageMixin._costFor(
        config, 1000, 100, cachedTokens=800, cacheHitMultiplier=0.25
    )
    actual = UsageMixin.calculateCost(
        config, 1000, 100, cachedTokens=800, cacheHitMultiplier=0.25
    )

    assert actual == expected


# ---------------------------------------------------------------------------
# unknown usage (a provider that reports no usage is NOT a free call)
# ---------------------------------------------------------------------------


async def test_complete_without_usage_is_recorded_as_unknown() -> None:
    """0/0 用量 = 「provider 没告诉我花了多少」，不是「这次免费」。

    记成 ESTIMATED/cost 0 会让 run 汇总出一个「已知的完整账单」，
    而账单里的数字是编的 —— 这正是绑定规则禁止的。
    """
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(
        response=LlmResponse(
            content="ok",
            modelName="test-model",
            promptTokens=0,
            completionTokens=0,
            totalTokens=0,
        )
    )

    await _client(fake, recorder).complete([LlmMessage(role="user", content="hi")])

    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0
    assert usage["cost"] == Decimal("0")
    # 未知的用量不得走定价路径。
    assert recorder.costCalls == []


async def test_complete_stream_with_zero_terminal_usage_is_recorded_as_unknown() -> None:
    """ollama 截断流：终止块 token 数为 0 —— 用量未知，不得记成 0 成本的 ESTIMATED。"""
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(
        chunks=[
            StreamChunk(content="a", isDone=False, promptTokens=0, completionTokens=0, modelName="m"),
            StreamChunk(content="", isDone=True, promptTokens=0, completionTokens=0, modelName="m"),
        ]
    )

    chunks = [
        chunk
        async for chunk in _client(fake, recorder).completeStream(
            [LlmMessage(role="user", content="hi")]
        )
    ]

    assert len(chunks) == 2
    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0
    assert recorder.costCalls == []


async def test_complete_with_tools_with_zero_usage_is_recorded_as_unknown() -> None:
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(
        toolsResponse=LlmResponseWithTools(
            content="done",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
        )
    )

    await _client(fake, recorder).complete_with_tools(
        [LlmMessage(role="user", content="hi")]
    )

    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "UNKNOWN"
    assert recorder.costCalls == []


async def test_non_llm_exception_with_a_tokens_attribute_is_recorded_as_unknown() -> None:
    """只有 LlmClientError 携带可信用量；别的异常有个 .tokens 属性也不作数。"""

    class _WeirdError(Exception):
        def __init__(self) -> None:
            super().__init__("weird")
            self.tokens = (99, 99)

    recorder = _FakeRecorder()
    fake = _FakeLlmClient(error=_WeirdError())

    with pytest.raises(_WeirdError):
        await _client(fake, recorder).complete([LlmMessage(role="user", content="hi")])

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0


# ---------------------------------------------------------------------------
# streaming edge cases
# ---------------------------------------------------------------------------


async def test_stream_failure_after_the_terminal_chunk_records_one_success_row() -> None:
    """终止块已见 ⇒ 用量已知；provider 之后再抛错不得再记一次失败。"""
    recorder = _FakeRecorder()
    fake = _FakeLlmClient(streamError=LlmClientError("boom after done"))

    with pytest.raises(LlmClientError):
        async for _chunk in _client(fake, recorder).completeStream(
            [LlmMessage(role="user", content="hi")]
        ):
            pass

    assert len(recorder.usageCalls) == 1
    usage = recorder.lastUsage
    assert usage["callStatus"] == "SUCCEEDED"
    assert usage["costSource"] == "ESTIMATED"
    assert usage["promptTokens"] == 21
    assert usage["completionTokens"] == 9


async def test_abandoned_stream_still_records_a_usage_row() -> None:
    """消费者提前放弃（客户端断线）：这次花费也必须有台账。"""
    recorder = _FakeRecorder()
    fake = _FakeLlmClient()
    client = _client(fake, recorder)

    stream = client.completeStream([LlmMessage(role="user", content="hi")])
    async for _chunk in stream:
        break
    await stream.aclose()
    await client.flushPendingRecords()

    usage = recorder.lastUsage
    assert usage["callStatus"] == "FAILED"
    assert usage["costSource"] == "UNKNOWN"
    assert usage["promptTokens"] == 0
    assert usage["completionTokens"] == 0
