"""L4 token 构成探针单元测试（feat/l4-token-profiling）。

覆盖：
- summarizeRequest 按角色/工具名的字符聚合与转义开销
- estimateRequestTokens 组件估值（组件和 = 总估值）
- buildIterationProfile 的 usage/cachedTokens 口径（None ≠ 0）与不可变性
- logIterationProfile 单行可解析
- buildToolResultProfile raw vs sent（转义放大）
- parseProfileLine 对裸行 / docker json-file 包装 / 噪声行的解析
"""

import json
import logging

import pytest

from app.infrastructure.llm.base_client import LlmMessage, LlmResponseWithTools, ToolCall
from app.security.prompt_sanitizer import UntrustedLabel, wrapUntrusted
from app.services import l4_token_profile as prof
from app.services.agent_tools_nl2sql import encodeExecuteSqlResult

TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "execute_sql", "parameters": {"sql": "string"}}},
    {"type": "function", "function": {"name": "list_joins", "parameters": {}}},
]


def _assistantWithToolCalls() -> LlmMessage:
    return LlmMessage(
        role="assistant",
        content="让我先查一下表结构",
        tool_calls=(
            {"id": "call_1", "name": "execute_sql", "args": {"sql": "SELECT 1"}},
            {"id": "call_2", "name": "list_joins", "args": {}},
        ),
    )


def _toolMessage(name: str, content: str, call_id: str = "call_1") -> LlmMessage:
    return LlmMessage(role="tool", content=content, tool_call_id=call_id, name=name)


def _sampleMessages() -> list[LlmMessage]:
    return [
        LlmMessage(role="system", content="系统提示词"),
        LlmMessage(role="user", content="为什么供应商供货量差异大？"),
        _assistantWithToolCalls(),
        _toolMessage("execute_sql", '{"rows": ["a", "b"], "row_count": 2}'),
        _toolMessage("list_joins", "[]", call_id="call_2"),
    ]


class TestSummarizeRequest:
    def test_role_breakdown_counts_exact_chars(self):
        summary = prof.summarizeRequest(_sampleMessages(), TOOL_SCHEMAS)

        assert summary["message_count"] == 5
        assert summary["chars"]["system"] == len("系统提示词")
        assert summary["chars"]["user"] == len("为什么供应商供货量差异大？")
        # assistant = content + 序列化后的 tool_calls
        assert summary["chars"]["assistant"] > len("让我先查一下表结构")
        assert summary["chars"]["tool"] == len('{"rows": ["a", "b"], "row_count": 2}') + len("[]")

    def test_tool_results_aggregated_by_name(self):
        summary = prof.summarizeRequest(_sampleMessages(), TOOL_SCHEMAS)

        assert summary["tools"]["execute_sql"]["count"] == 1
        assert summary["tools"]["execute_sql"]["chars"] == len('{"rows": ["a", "b"], "row_count": 2}')
        assert summary["tools"]["list_joins"]["chars"] == len("[]")

    def test_tool_message_without_name_goes_unknown(self):
        messages = [LlmMessage(role="tool", content="x", tool_call_id="c1", name=None)]

        summary = prof.summarizeRequest(messages, TOOL_SCHEMAS)

        assert summary["tools"]["unknown"]["count"] == 1
        assert summary["tools"]["unknown"]["chars"] == 1

    def test_escape_chars_counts_html_entities(self):
        # &quot; 长度 6、原字符 1 ⇒ 每处净增 5
        content = 'a&quot;b&quot;c'
        messages = [_toolMessage("execute_sql", content)]

        summary = prof.summarizeRequest(messages, TOOL_SCHEMAS)

        assert summary["tools"]["execute_sql"]["escapeChars"] == 10

    def test_total_chars_is_sum_of_parts(self):
        summary = prof.summarizeRequest(_sampleMessages(), TOOL_SCHEMAS)

        parts = sum(summary["chars"].values()) + summary["tool_schema_chars"]
        assert summary["total_chars"] == parts

    def test_empty_messages_does_not_raise(self):
        summary = prof.summarizeRequest([], TOOL_SCHEMAS)

        assert summary["message_count"] == 0
        assert summary["total_chars"] == summary["tool_schema_chars"]


class TestBuildIterationProfile:
    def test_usage_present_calibrates_scale(self):
        profile = prof.buildIterationProfile(
            runId="r1",
            sessionId="s1",
            iteration=2,
            modelName="deepseek-chat",
            messages=_sampleMessages(),
            toolSchemas=TOOL_SCHEMAS,
            usage={"prompt_tokens": 1000, "completion_tokens": 50},
            cachedTokens=800,
        )

        assert profile.providerPromptTokens == 1000
        assert profile.providerCompletionTokens == 50
        assert profile.cachedTokens == 800
        assert profile.cacheHitRate == pytest.approx(0.8)
        assert profile.usagePresent is True
        assert profile.scale == pytest.approx(1000 / profile.estimatedTotalTokens)
        assert profile.iteration == 2
        assert profile.runId == "r1"
        assert profile.sessionId == "s1"

    def test_usage_none_stays_none(self):
        profile = prof.buildIterationProfile(
            runId="r1",
            sessionId=None,
            iteration=1,
            modelName="deepseek-chat",
            messages=_sampleMessages(),
            toolSchemas=TOOL_SCHEMAS,
            usage=None,
        )

        assert profile.usagePresent is False
        assert profile.providerPromptTokens is None
        assert profile.providerCompletionTokens is None
        assert profile.cachedTokens is None
        assert profile.cacheHitRate is None
        assert profile.scale is None
        # 估值照常产出（字符遥测不依赖 usage）
        assert profile.estimatedTotalTokens > 0

    def test_cached_zero_is_not_none(self):
        profile = prof.buildIterationProfile(
            runId="r1",
            sessionId=None,
            iteration=1,
            modelName="deepseek-chat",
            messages=_sampleMessages(),
            toolSchemas=TOOL_SCHEMAS,
            usage={"prompt_tokens": 100, "completion_tokens": 10},
            cachedTokens=0,
        )

        assert profile.cachedTokens == 0
        assert profile.cacheHitRate == 0.0

    def test_components_sum_to_estimated_total(self):
        profile = prof.buildIterationProfile(
            runId="r1",
            sessionId=None,
            iteration=1,
            modelName="deepseek-chat",
            messages=_sampleMessages(),
            toolSchemas=TOOL_SCHEMAS,
            usage=None,
        )

        total = sum(c.estimatedTokens for c in profile.components)
        assert total == profile.estimatedTotalTokens
        names = [c.name for c in profile.components]
        # 工具结果按 name 拆分为独立组件
        assert "tool:execute_sql" in names
        assert "tool:list_joins" in names
        for comp in profile.components:
            assert comp.estimatedTokens > 0

    def test_does_not_mutate_inputs(self):
        messages = _sampleMessages()
        schemas = [dict(s) for s in TOOL_SCHEMAS]
        before_msgs = list(messages)
        before_schemas = json.dumps(schemas, ensure_ascii=False)

        prof.buildIterationProfile(
            runId="r1",
            sessionId=None,
            iteration=1,
            modelName="deepseek-chat",
            messages=messages,
            toolSchemas=schemas,
            usage={"prompt_tokens": 10, "completion_tokens": 1},
        )

        assert messages == before_msgs
        assert json.dumps(schemas, ensure_ascii=False) == before_schemas


class TestLogging:
    def test_log_iteration_profile_emits_single_parsable_line(self, caplog):
        profile = prof.buildIterationProfile(
            runId="r1",
            sessionId="s1",
            iteration=1,
            modelName="deepseek-chat",
            messages=_sampleMessages(),
            toolSchemas=TOOL_SCHEMAS,
            usage={"prompt_tokens": 100, "completion_tokens": 5},
        )

        with caplog.at_level(logging.INFO, logger="app.services.l4_token_profile"):
            prof.logIterationProfile(profile)

        records = [r for r in caplog.records if prof.TOKEN_PROFILE_TAG in r.getMessage()]
        assert len(records) == 1
        line = records[0].getMessage()
        assert "\n" not in line
        parsed = prof.parseProfileLine(line)
        assert parsed is not None
        assert parsed["iteration"] == 1
        assert parsed["providerPromptTokens"] == 100

    def test_log_tool_result_single_line(self, caplog):
        raw = '{"rows": ["x"], "row_count": 1}'
        sent = wrapUntrusted(UntrustedLabel.AGENT_TOOL_RESULT, raw)
        record = prof.buildToolResultProfile(
            runId="r1",
            sessionId="s1",
            iteration=1,
            toolCallId="call_1",
            toolName="execute_sql",
            rawContent=raw,
            sentContent=sent,
            rowCount=1,
        )

        with caplog.at_level(logging.INFO, logger="app.services.l4_token_profile"):
            prof.logToolResultProfile(record)

        line = [r.getMessage() for r in caplog.records if prof.TOOL_RESULT_TAG in r.getMessage()][0]
        assert "\n" not in line
        parsed = prof.parseProfileLine(line)
        assert parsed["event"] == "tool_result"
        assert parsed["rawChars"] == len(raw)
        assert parsed["sentChars"] == len(sent)
        assert parsed["sentChars"] > parsed["rawChars"]
        assert parsed["escapeExpansion"] > 1
        assert parsed["rowCount"] == 1


class TestBuildLoopEndProfile:
    def test_loop_end_carries_totals(self):
        record = prof.buildLoopEndProfile(
            runId="r1",
            sessionId="s1",
            modelName="deepseek-chat",
            iterations=3,
            terminatedReason="answered",
            promptTokens=600,
            completionTokens=60,
            cachedTokens=100,
        )

        assert record["event"] == "loop_end"
        assert record["runId"] == "r1"
        assert record["sessionId"] == "s1"
        assert record["iterations"] == 3
        assert record["terminatedReason"] == "answered"
        assert record["promptTokens"] == 600
        assert record["cachedTokens"] == 100

    def test_loop_end_cached_none_preserved(self):
        record = prof.buildLoopEndProfile(
            runId="r1",
            sessionId=None,
            modelName="deepseek-chat",
            iterations=1,
            terminatedReason="error",
            promptTokens=10,
            completionTokens=0,
            cachedTokens=None,
        )

        assert record["cachedTokens"] is None


class TestParseProfileLine:
    def test_parses_bare_log_line(self):
        payload = json.dumps({"event": "iteration", "iteration": 1}, ensure_ascii=False)
        line = f"2026-10-31 10:00:00 INFO app.services.l4_token_profile: {prof.TOKEN_PROFILE_TAG} {payload}"

        parsed = prof.parseProfileLine(line)

        assert parsed == {"event": "iteration", "iteration": 1}

    def test_parses_docker_json_wrapper(self):
        inner = f'{prof.TOOL_RESULT_TAG} {json.dumps({"event": "tool_result", "rowCount": 3})}'
        wrapper = json.dumps({"log": inner, "stream": "stdout"})

        parsed = prof.parseProfileLine(wrapper)

        assert parsed is not None
        assert parsed["event"] == "tool_result"

    def test_unrelated_line_returns_none(self):
        assert prof.parseProfileLine("INFO GET /api/v1/chat 200") is None

    def test_truncated_json_returns_none(self):
        line = f"{prof.TOKEN_PROFILE_TAG} " + '{"event": "iteration", "providerPromptTo'

        assert prof.parseProfileLine(line) is None

    def test_empty_line_returns_none(self):
        assert prof.parseProfileLine("   ") is None


class _FakeExecutor:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)

    async def execute_read_only(self, sql):
        return self.result_sets.pop(0)


def _fakeResponses():
    """三轮 fake：execute_sql → execute_sql → answered。"""
    return [
        LlmResponseWithTools(
            content=None,
            tool_calls=[ToolCall(id="t1", name="execute_sql", args={"sql": "SELECT 1 AS x"})],
            usage={"prompt_tokens": 100, "completion_tokens": 20},
            model="deepseek-chat",
        ),
        LlmResponseWithTools(
            content=None,
            tool_calls=[ToolCall(id="t2", name="execute_sql", args={"sql": "SELECT 2 AS y"})],
            usage={"prompt_tokens": 200, "completion_tokens": 30},
            model="deepseek-chat",
        ),
        LlmResponseWithTools(
            content="答案是 1 和 2。",
            tool_calls=[],
            usage={"prompt_tokens": 300, "completion_tokens": 10},
            model="deepseek-chat",
        ),
    ]


def _installFixedEstimator(monkeypatch):
    """把 tiktoken 估值换成固定组件，隔离 tokenizer 差异、加速运行时测试。"""
    fixed = (
        prof.ComponentStats("system", 10, 10),
        prof.ComponentStats("user", 20, 20),
    )
    monkeypatch.setattr(prof, "estimateRequestTokens", lambda *a, **k: fixed)


async def _runFakeLoop(monkeypatch, caplog, *, sessionId="sess-1"):
    from unittest.mock import AsyncMock

    from app.services.agent_runtime_service import AgentRuntimeService

    _installFixedEstimator(monkeypatch)
    responses = _fakeResponses()
    llm = type("FakeLlm", (), {})()
    llm.complete_with_tools = AsyncMock(side_effect=lambda **kwargs: responses.pop(0))
    executor = _FakeExecutor([[{"x": 1}], [{"y": 2}]])

    with caplog.at_level(logging.INFO):
        result = await AgentRuntimeService().run_agent_loop(
            session=None,
            user_id=1,
            question="为什么供货量差异大？",
            llm_client=llm,
            executor=executor,
            ontology=None,
            sessionId=sessionId,
        )
    return result


class TestLoopWiring:
    """run_agent_loop 接线：逐轮 profile / 工具结果行 / loop_end 汇总。"""

    async def test_loop_logs_profile_per_iteration_and_loop_end(self, monkeypatch, caplog):
        result = await _runFakeLoop(monkeypatch, caplog)

        assert result.prompt_tokens == 600
        assert result.completion_tokens == 60
        assert result.terminated_reason == "answered"
        assert result.iterations_used == 3

        lines = [r.getMessage() for r in caplog.records if prof.TOKEN_PROFILE_TAG in r.getMessage()]
        parsed = [prof.parseProfileLine(line) for line in lines]
        iterations = [p for p in parsed if p and p["event"] == "iteration"]
        loopEnds = [p for p in parsed if p and p["event"] == "loop_end"]

        assert [p["iteration"] for p in iterations] == [1, 2, 3]
        assert [p["providerPromptTokens"] for p in iterations] == [100, 200, 300]
        runIds = {p["runId"] for p in iterations}
        assert len(runIds) == 1
        assert all(isinstance(rid, str) and rid for rid in runIds)
        assert {p["sessionId"] for p in iterations} == {"sess-1"}
        assert len(loopEnds) == 1
        assert loopEnds[0]["promptTokens"] == 600
        assert loopEnds[0]["terminatedReason"] == "answered"

    async def test_tool_result_log_records_raw_sent_and_row_count(self, monkeypatch, caplog):
        await _runFakeLoop(monkeypatch, caplog)

        lines = [r.getMessage() for r in caplog.records if prof.TOOL_RESULT_TAG in r.getMessage()]
        parsed = [prof.parseProfileLine(line) for line in lines]

        assert len(parsed) == 2
        byIteration = {p["iteration"]: p for p in parsed}
        first = byIteration[1]
        expectedRaw = json.dumps(
            encodeExecuteSqlResult([{"x": 1}]), ensure_ascii=False, default=str
        )
        assert first["rawChars"] == len(expectedRaw)
        assert first["sentChars"] > first["rawChars"]
        assert first["escapeExpansion"] > 1
        assert first["rowCount"] == 1
        assert first["toolName"] == "execute_sql"
        assert first["runId"] == byIteration[2]["runId"]

    async def test_profile_failure_does_not_break_loop(self, monkeypatch, caplog):
        def _boom(**kwargs):
            raise RuntimeError("profile exploded")

        monkeypatch.setattr(prof, "buildIterationProfile", _boom)

        result = await _runFakeLoop(monkeypatch, caplog)

        assert result.terminated_reason == "answered"
        assert result.prompt_tokens == 600
        assert result.completion_tokens == 60
        warnings = [r for r in caplog.records if "token_profile_failed" in r.getMessage()]
        assert warnings, "profile 失败必须留 warning，不允许静默吞掉"

    async def test_loop_works_without_session_id(self, monkeypatch, caplog):
        result = await _runFakeLoop(monkeypatch, caplog, sessionId=None)

        assert result.terminated_reason == "answered"
        lines = [r.getMessage() for r in caplog.records if prof.TOKEN_PROFILE_TAG in r.getMessage()]
        parsed = [prof.parseProfileLine(line) for line in lines]
        iterations = [p for p in parsed if p and p["event"] == "iteration"]
        assert all(p["sessionId"] is None for p in iterations)
