"""L4 Agent Loop — integration tests.

主要逻辑是 LLM-driven，用 mock llm_client + mock executor
而非真实 LLM API 调用。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.infrastructure.llm.base_client import ToolCall
from app.services.agent_runtime_service import AgentRuntimeService, AgentLoopResult


@pytest.fixture
def mock_llm():
    """Mock BaseLlmClient，按 calls 顺序返回不同 response。"""
    from unittest.mock import MagicMock
    return MagicMock()


@pytest.fixture
def mock_executor():
    """Mock SQL executor。"""
    from unittest.mock import MagicMock, AsyncMock
    executor = MagicMock()
    executor.execute_read_only = AsyncMock(return_value=[{"x": 1}])
    return executor


@pytest.fixture
def mock_ontology():
    """Mock Ontology 服务。"""
    from unittest.mock import MagicMock, AsyncMock
    ontology = MagicMock()
    ontology.listTables = AsyncMock(return_value=["THBI.PO_HEADER"])
    ontology.describeTable = AsyncMock(return_value=[{"name": "id"}])
    ontology.sampleRows = AsyncMock(return_value=[])
    ontology.listJoins = AsyncMock(return_value=[])
    return ontology


@pytest.fixture
def service():
    return AgentRuntimeService()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_loop_converges_in_max_iterations(
    mock_llm, mock_executor, mock_ontology, service
):
    """LLM 三次决策：先 list_tables，再 execute_sql，再 final answer。"""

    async def complete_with_tools_side_effect(messages, tools=None, tool_choice="auto"):
        # 模拟 LLM 的三次响应
        # 第一次：返回 list_tables tool_call
        # 第二次：返回 execute_sql tool_call
        # 第三次：无 tool_call，返回最终答案
        call_count = complete_with_tools_side_effect.call_count
        complete_with_tools_side_effect.call_count += 1

        from unittest.mock import MagicMock
        from app.infrastructure.llm.base_client import LlmResponseWithTools

        if call_count == 0:
            # 第一次：LLM 想先看看有哪些表
            return LlmResponseWithTools(
                content=None,
                tool_calls=[ToolCall(id="t1", name="list_tables", args={})],
                usage={"prompt_tokens": 100, "completion_tokens": 20},
                model="test",
            )
        elif call_count == 1:
            # 第二次：LLM 执行 SQL
            return LlmResponseWithTools(
                content=None,
                tool_calls=[ToolCall(id="t2", name="execute_sql", args={"sql": "SELECT 1 AS x"})],
                usage={"prompt_tokens": 200, "completion_tokens": 30},
                model="test",
            )
        else:
            # 第三次：返回答案，包含 final_sql 标记
            return LlmResponseWithTools(
                content="Answer is 1.\n\n```final_sql\nSELECT 1 AS x\n```",
                tool_calls=[],
                usage={"prompt_tokens": 300, "completion_tokens": 10},
                model="test",
            )

    complete_with_tools_side_effect.call_count = 0
    mock_llm.complete_with_tools = AsyncMock(side_effect=complete_with_tools_side_effect)

    result: AgentLoopResult = await service.run_agent_loop(
        session=MagicMock(),  # session 未被 mock executor 路径使用
        user_id=1,
        question="What is X?",
        llm_client=mock_llm,
        executor=mock_executor,
        ontology=mock_ontology,
    )

    assert result.final_sql == "SELECT 1 AS x"
    assert result.iterations_used == 3
    assert result.terminated_reason == "answered"
    assert "list_tables" in result.tool_calls_made
    assert "execute_sql" in result.tool_calls_made


@pytest.mark.asyncio
async def test_agent_loop_max_iterations_cap(
    mock_llm, mock_executor, mock_ontology, service
):
    """max_iterations=3 时强制终止。"""

    # LLM 永远返回 describe_table tool_call（永不停止）
    async def never_stop(messages, tools=None, tool_choice="auto"):
        never_stop.call_count += 1
        from app.infrastructure.llm.base_client import LlmResponseWithTools

        return LlmResponseWithTools(
            content=None,
            tool_calls=[ToolCall(id=f"t{never_stop.call_count}", name="describe_table", args={"table_name": "x"})],
            usage={"prompt_tokens": 100, "completion_tokens": 10},
            model="test",
        )

    never_stop.call_count = 0
    mock_llm.complete_with_tools = AsyncMock(side_effect=never_stop)

    result: AgentLoopResult = await service.run_agent_loop(
        session=MagicMock(),
        user_id=1,
        question="Q",
        max_iterations=3,
        llm_client=mock_llm,
        executor=mock_executor,
        ontology=mock_ontology,
    )

    assert result.iterations_used == 3
    assert result.terminated_reason == "max_iterations"
    # describe_table 被调用了 3 次
    assert result.tool_calls_made.count("describe_table") == 3


@pytest.mark.asyncio
async def test_agent_loop_executes_tools(
    mock_llm, mock_executor, mock_ontology, service
):
    """execute_sql tool_call 被实际 dispatch 执行。"""

    async def two_calls(messages, tools=None, tool_choice="auto"):
        two_calls.call_count += 1
        from app.infrastructure.llm.base_client import LlmResponseWithTools

        if two_calls.call_count == 1:
            return LlmResponseWithTools(
                content=None,
                tool_calls=[ToolCall(id="t1", name="execute_sql", args={"sql": "SELECT 1"})],
                usage={"prompt_tokens": 100, "completion_tokens": 20},
                model="test",
            )
        else:
            return LlmResponseWithTools(
                content="Done.",
                tool_calls=[],
                usage={"prompt_tokens": 200, "completion_tokens": 5},
                model="test",
            )

    two_calls.call_count = 0
    mock_llm.complete_with_tools = AsyncMock(side_effect=two_calls)

    result: AgentLoopResult = await service.run_agent_loop(
        session=MagicMock(),
        user_id=1,
        question="Q",
        llm_client=mock_llm,
        executor=mock_executor,
        ontology=mock_ontology,
    )

    assert "execute_sql" in result.tool_calls_made
    mock_executor.execute_read_only.assert_called()


@pytest.mark.asyncio
async def test_agent_loop_cost_cap(
    mock_llm, mock_executor, mock_ontology, service
):
    """累计 cost 超 budget_usd 时强制终止。"""

    # 每次调用 token 量极大：1M prompt + 1M completion → ~0.75 USD/call
    # cost_budget=0.5 → 第一次 0.75 > 0.5，第二次调用前触发 cost_cap
    async def expensive(messages, tools=None, tool_choice="auto"):
        expensive.call_count += 1
        from app.infrastructure.llm.base_client import LlmResponseWithTools

        return LlmResponseWithTools(
            content=None,
            tool_calls=[ToolCall(id=f"t{expensive.call_count}", name="list_tables", args={})],
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="test",
        )

    expensive.call_count = 0
    mock_llm.complete_with_tools = AsyncMock(side_effect=expensive)

    result: AgentLoopResult = await service.run_agent_loop(
        session=MagicMock(),
        user_id=1,
        question="Q",
        cost_budget_usd=0.3,
        llm_client=mock_llm,
        executor=mock_executor,
        ontology=mock_ontology,
    )

    assert result.terminated_reason == "cost_cap"
    # 第一次 cost=0.75 > 0.3 budget，在第二次调用前触发 cost_cap
    assert result.iterations_used == 1
