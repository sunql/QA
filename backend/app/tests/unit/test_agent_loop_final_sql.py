"""L4 agent loop 的 final_sql 采集与迭代预算（2026-10-31）。

背景：L4 路径此前只在模型主动输出 ``final_sql`` 围栏时才回填 final_sql
（``_extract_final_sql``），而系统提示词从未要求该格式 ⇒ 前端永远拿不到 SQL
（生产实证：L4 回答的 ``sql_generated`` 恒为 NULL，尽管 execute_sql 确实执行过）。

本文件钉住这些事：
1. 未输出围栏时，兜底采集「最后一条成功执行的 execute_sql」；
2. 模型显式输出围栏时，围栏仍然优先（既有行为不回归）；
3. 执行失败的 execute_sql 不进 final_sql（错误 SQL 不能冒充「用过的 SQL」）；
4. 先成功后失败 / 一轮多条 SQL 时，采集口径仍是「最后一条成功的」；
5. 未收敛（max_iterations）也回显 SQL；
6. 默认迭代预算是 10 —— 「为什么」类归因问题实测 5 轮只够探表 + 试错 SQL。
"""

from unittest.mock import AsyncMock

from app.infrastructure.llm.base_client import LlmResponseWithTools, ToolCall
from app.services.agent_runtime_service import AgentRuntimeService


class _FakeExecutor:
    """执行器替身：记录 SQL，按序返回结果集或抛错。"""

    def __init__(self, results=None, error: Exception | None = None, errors=None):
        self.results = list(results or [])
        self.error = error
        # 按调用序指定逐条异常（None = 该次成功）；未列出的调用沿用 ``error``
        self.errors = list(errors or [])
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str):
        self.calls.append(sql)
        index = len(self.calls) - 1
        fail = self.errors[index] if index < len(self.errors) else self.error
        if fail is not None:
            raise fail
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


async def _run(responses, executor):
    return await AgentRuntimeService().run_agent_loop(
        session=None,
        user_id=1,
        question="为什么今年5月份各供应商的供货量与往月比差距大",
        llm_client=_fakeLlm(responses),
        executor=executor,
        ontology=None,
    )


class TestFinalSqlCapture:
    async def test_captures_last_executed_sql_when_answer_has_no_fence(self):
        executor = _FakeExecutor([[{"x": 1}], [{"y": 2}]])

        result = await _run(
            [
                _toolCallStep("execute_sql", {"sql": "SELECT 1 AS x"}, "t1"),
                _toolCallStep("execute_sql", {"sql": "SELECT 2 AS y"}, "t2"),
                _answerStep("5 月供货量整体跃升，差异来自两家供应商。"),
            ],
            executor,
        )

        assert result.terminated_reason == "answered"
        assert result.final_sql == "SELECT 2 AS y"

    async def test_explicit_final_sql_fence_still_wins(self):
        executor = _FakeExecutor([[{"x": 1}]])

        result = await _run(
            [
                _toolCallStep("execute_sql", {"sql": "SELECT 1 AS x"}, "t1"),
                _answerStep("结论如下。\n```final_sql\nSELECT 9 AS authoritative\n```"),
            ],
            executor,
        )

        assert result.final_sql == "SELECT 9 AS authoritative"

    async def test_failed_execute_sql_is_not_captured(self):
        executor = _FakeExecutor(error=RuntimeError("syntax error"))

        result = await _run(
            [
                _toolCallStep("execute_sql", {"sql": "SELECT broken"}, "t1"),
                _answerStep("数据不足，先给推断。"),
            ],
            executor,
        )

        assert result.terminated_reason == "answered"
        assert result.final_sql is None

    async def test_failure_after_success_keeps_the_successful_sql(self):
        """最后一次失败不能抹掉此前成功的那条。"""
        executor = _FakeExecutor([[{"x": 1}]], errors=[None, RuntimeError("syntax error")])

        result = await _run(
            [
                _toolCallStep("execute_sql", {"sql": "SELECT 1 AS x"}, "t1"),
                _toolCallStep("execute_sql", {"sql": "SELECT broken"}, "t2"),
                _answerStep("结论如下。"),
            ],
            executor,
        )

        assert result.final_sql == "SELECT 1 AS x"

    async def test_multiple_sql_in_one_iteration_takes_the_last(self):
        """一轮里并排多条 execute_sql 时取最后一条（与执行顺序一致）。"""
        executor = _FakeExecutor([[{"a": 1}], [{"b": 2}]])
        llm = _fakeLlm(
            [
                LlmResponseWithTools(
                    content=None,
                    tool_calls=[
                        ToolCall(id="t1", name="execute_sql", args={"sql": "SELECT 1"}),
                        ToolCall(id="t2", name="execute_sql", args={"sql": "SELECT 2"}),
                    ],
                    usage={"prompt_tokens": 10, "completion_tokens": 5},
                    model="deepseek-chat",
                ),
                _answerStep("结论如下。"),
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

        assert result.final_sql == "SELECT 2"


class TestIterationBudget:
    async def test_default_budget_allows_ten_iterations(self):
        """不传 max_iterations 时默认 10 轮（5 轮不够探表 + 试错 SQL）。"""
        executor = _FakeExecutor()
        llm = _fakeLlm(
            [_toolCallStep("execute_sql", {"sql": "SELECT 1"}) for _ in range(10)]
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
        assert result.iterations_used == 10

    async def test_max_iterations_still_surfaces_last_executed_sql(self):
        """未收敛也回显 SQL：用户至少要能看到「问到哪里、查了什么」。"""
        executor = _FakeExecutor([[{"x": 1}] for _ in range(3)])
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
        assert result.final_sql == "SELECT 1 AS x"
