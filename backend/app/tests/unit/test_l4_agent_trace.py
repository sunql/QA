"""L4 执行轨迹（Agent Trace）单测。

L4 此前只把工具名列表交给调用方，参数 / 返回摘要 / 耗时 / 成败全丢，
前端只能看到一个黑盒回答。本文件钉住「事后执行轨迹卡」的数据契约：

1. 五种工具的 args / resultSummary 摘要形状；
2. execute_sql 的 SQL 预览归一 + 截断，错误文本截断；
3. 快照永不携带原始 rows / 工具 content；
4. 步数上限与 truncated 标记；
5. ``_dispatchSingleTool`` 在工具执行后追加一步；
6. ``run_agent_loop`` 在 answered / max_iterations 两条路径都带出轨迹。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.infrastructure.llm.base_client import LlmResponseWithTools, ToolCall
from app.services import l4_trace
from app.services.agent_runtime_service import AgentRuntimeService, _dispatchSingleTool


class _FakeExecutor:
    """执行器替身：按序返回结果集或抛错。"""

    def __init__(self, results=None, error: Exception | None = None):
        self.results = list(results or [])
        self.error = error
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str):
        self.calls.append(sql)
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else []


def _toolCallStep(name: str, args: dict, callId: str = "t1") -> LlmResponseWithTools:
    return LlmResponseWithTools(
        content=None,
        tool_calls=[ToolCall(id=callId, name=name, args=args)],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        model="deepseek-chat",
    )


def _answerStep(text: str) -> LlmResponseWithTools:
    return LlmResponseWithTools(
        content=text,
        tool_calls=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        model="deepseek-chat",
    )


def _fakeLlm(responses) -> object:
    llm = type("FakeLlm", (), {})()
    llm.complete_with_tools = AsyncMock(side_effect=lambda **kwargs: responses.pop(0))
    return llm


def _fakeLlmRaisingAfter(responses) -> object:
    """side_effect 为列表时，异常实例会被抛出（而不是当作返回值）。"""
    llm = type("FakeLlm", (), {})()
    llm.complete_with_tools = AsyncMock(side_effect=list(responses))
    return llm


class TestSummarizeArgs:
    def test_execute_sql_preview_collapses_whitespace_and_truncates(self):
        longSql = "SELECT\n  a,\n  b\nFROM t WHERE x = 1 " + "AND y = 2 " * 80

        summary = l4_trace.summarizeArgs("execute_sql", {"sql": longSql})

        preview = summary["sqlPreview"]
        assert "\n" not in preview
        assert len(preview) <= l4_trace.MAX_SQL_PREVIEW_CHARS

    def test_execute_sql_preview_keeps_short_sql_intact(self):
        summary = l4_trace.summarizeArgs("execute_sql", {"sql": "SELECT 1 FROM dual"})
        assert summary == {"sqlPreview": "SELECT 1 FROM dual"}

    def test_describe_table_keeps_table_name(self):
        summary = l4_trace.summarizeArgs(
            "describe_table", {"table_name": "DWD_GOODS_RECEIPT_DTL"}
        )
        assert summary == {"table": "DWD_GOODS_RECEIPT_DTL"}

    def test_list_joins_caps_tables_list(self):
        tables = [f"T{i}" for i in range(10)]
        summary = l4_trace.summarizeArgs("list_joins", {"tables": tables})
        assert summary["tables"] == tables[:5]

    def test_unknown_tool_args_are_not_echoed(self):
        summary = l4_trace.summarizeArgs("evil_tool", {"secret": "s3cr3t"})
        assert summary == {}


class TestSummarizeResult:
    def test_list_tables_counts_tables(self):
        summary, error = l4_trace.summarizeResult(
            "list_tables", json.dumps({"tables": ["A", "B", "C"]}), isError=False
        )
        assert summary == {"tables": 3}
        assert error is None

    def test_describe_table_counts_columns(self):
        content = json.dumps({"table": "T", "columns": [[1], [2], [3]]})
        summary, error = l4_trace.summarizeResult(
            "describe_table", content, isError=False
        )
        assert summary == {"columns": 3}
        assert error is None

    def test_sample_rows_counts_rows(self):
        content = json.dumps({"rows": [{"a": 1}, {"a": 2}], "limit": 5})
        summary, _ = l4_trace.summarizeResult("sample_rows", content, isError=False)
        assert summary == {"rows": 2}

    def test_list_joins_uses_reported_count(self):
        content = json.dumps({"count": 7, "edges": ["a", "b"]})
        summary, _ = l4_trace.summarizeResult("list_joins", content, isError=False)
        assert summary == {"joins": 7}

    def test_execute_sql_reports_true_row_count_and_truncation(self):
        content = json.dumps(
            {"columns": ["x"], "rows": [[1]], "row_count": 286, "truncated": True}
        )
        summary, error = l4_trace.summarizeResult(
            "execute_sql", content, isError=False
        )
        assert summary == {"rows": 286, "truncated": True}
        assert error is None

    def test_execute_sql_never_carries_raw_rows(self):
        content = json.dumps(
            {"columns": ["secret_col"], "rows": [["secret_value"]], "row_count": 1}
        )
        summary, _ = l4_trace.summarizeResult("execute_sql", content, isError=False)

        serialized = json.dumps(summary)
        assert "secret_value" not in serialized
        assert "secret_col" not in serialized

    def test_error_result_is_truncated_and_code_kept(self):
        content = json.dumps(
            {"error": "RuntimeError", "detail": "boom " * 200}
        )
        summary, error = l4_trace.summarizeResult(
            "execute_sql", content, isError=True
        )
        assert summary == {}
        assert error is not None
        assert error.startswith("RuntimeError")
        assert len(error) <= l4_trace.MAX_ERROR_CHARS


class TestBuildTrace:
    def _step(self, index: int) -> dict:
        return l4_trace.buildStep(
            index=index,
            iteration=1,
            tool="execute_sql",
            args={"sql": f"SELECT {index}"},
            rawContent=json.dumps({"row_count": index}),
            isError=False,
            elapsedMs=5,
        )

    def test_step_shape_is_camel_case_contract(self):
        step = self._step(0)
        assert step == {
            "index": 0,
            "iteration": 1,
            "tool": "execute_sql",
            "status": "ok",
            "elapsedMs": 5,
            "args": {"sqlPreview": "SELECT 0"},
            "resultSummary": {"rows": 0},
        }

    def test_error_step_sets_status_and_omits_error_when_ok(self):
        errorStep = l4_trace.buildStep(
            index=0,
            iteration=1,
            tool="execute_sql",
            args={"sql": "SELECT broken"},
            rawContent=json.dumps({"error": "SyntaxError", "detail": "bad sql"}),
            isError=True,
            elapsedMs=3,
        )
        assert errorStep["status"] == "error"
        assert errorStep["error"].startswith("SyntaxError")

        okStep = self._step(0)
        assert "error" not in okStep

    def test_trace_caps_steps_and_marks_truncated(self):
        steps = [self._step(i) for i in range(l4_trace.MAX_TRACE_STEPS + 5)]

        trace = l4_trace.buildTrace(
            steps=steps,
            terminatedReason="max_iterations",
            iterations=10,
            totalMs=1234,
        )

        assert trace["kind"] == "l4_agent_trace"
        assert trace["status"] == "max_iterations"
        assert trace["stepCount"] == l4_trace.MAX_TRACE_STEPS
        assert len(trace["steps"]) == l4_trace.MAX_TRACE_STEPS
        assert trace["truncated"] is True
        # 必须是可 JSON 序列化的纯数据（JSONB 落库前置条件）
        json.dumps(trace)


class TestDispatchCollectsTrace:
    async def test_dispatch_appends_step_for_execute_sql(self):
        tc = SimpleNamespace(id="t1", name="execute_sql", args={"sql": "SELECT 1"})
        executor = _FakeExecutor(results=[[{"x": 1}, {"x": 2}]])
        traceSteps: list[dict] = []

        await _dispatchSingleTool(
            tc=tc,
            session=None,
            executor=executor,
            iteration=2,
            trace_steps=traceSteps,
        )

        assert len(traceSteps) == 1
        step = traceSteps[0]
        assert step["tool"] == "execute_sql"
        assert step["iteration"] == 2
        assert step["status"] == "ok"
        assert step["resultSummary"]["rows"] == 2
        assert step["elapsedMs"] >= 0

    async def test_dispatch_marks_failed_execute_sql(self):
        tc = SimpleNamespace(id="t1", name="execute_sql", args={"sql": "SELECT broken"})
        executor = _FakeExecutor(error=RuntimeError("syntax error"))
        traceSteps: list[dict] = []

        await _dispatchSingleTool(
            tc=tc,
            session=None,
            executor=executor,
            iteration=1,
            trace_steps=traceSteps,
        )

        assert traceSteps[0]["status"] == "error"
        assert "syntax error" in traceSteps[0]["error"]


class TestRunAgentLoopTrace:
    async def test_answered_result_carries_trace(self):
        executor = _FakeExecutor(results=[[{"secret_col": "secret_value"}]])
        llm = _fakeLlm(
            [
                _toolCallStep("execute_sql", {"sql": "SELECT 1 AS x"}, "t1"),
                _answerStep("只有一个值。"),
            ]
        )

        result = await AgentRuntimeService().run_agent_loop(
            session=None,
            user_id=1,
            question="为什么今年5月份各供应商的供货量与往月比差距大",
            llm_client=llm,
            executor=executor,
            ontology=None,
        )

        assert result.terminated_reason == "answered"
        assert result.agent_trace is not None
        assert result.agent_trace["status"] == "answered"
        assert result.agent_trace["stepCount"] == 1
        assert result.agent_trace["steps"][0]["tool"] == "execute_sql"
        # 原始结果行不进快照（只留 row_count 摘要）
        serialized = json.dumps(result.agent_trace)
        assert "secret_value" not in serialized
        assert "secret_col" not in serialized

    async def test_max_iterations_result_still_carries_trace(self):
        executor = _FakeExecutor(results=[[{"x": 1}]])
        llm = _fakeLlm(
            [_toolCallStep("execute_sql", {"sql": "SELECT 1 AS x"}) for _ in range(10)]
        )

        result = await AgentRuntimeService().run_agent_loop(
            session=None,
            user_id=1,
            question="为什么今年5月份各供应商的供货量与往月比差距大",
            llm_client=llm,
            executor=executor,
            ontology=None,
        )

        assert result.terminated_reason == "max_iterations"
        assert result.agent_trace is not None
        assert result.agent_trace["status"] == "max_iterations"
        assert result.agent_trace["stepCount"] == 10

    async def test_error_termination_produces_no_trace(self):
        # error 会降级 L2/L3，轨迹卡无处挂载：此时必须不出快照，
        # 否则会在降级消息旁留下没有实际执行过程的孤儿卡。
        executor = _FakeExecutor(results=[[{"x": 1}]])
        llm = _fakeLlmRaisingAfter(
            [
                _toolCallStep("execute_sql", {"sql": "SELECT 1 AS x"}, "t1"),
                RuntimeError("upstream 500"),
            ]
        )

        result = await AgentRuntimeService().run_agent_loop(
            session=None,
            user_id=1,
            question="为什么今年5月份各供应商的供货量与往月比差距大",
            llm_client=llm,
            executor=executor,
            ontology=None,
        )

        assert result.terminated_reason == "error"
        assert result.agent_trace is None
