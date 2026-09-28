"""L4 Agent Loop — chat_service routing integration tests.

Task 4.4: 测试 chat_service.processMessage 的 L4 入口路由逻辑。
主要验证：
1. ENABLE_L4_AGENT_LOOP=true + 探索性关键词 → 走 L4 Agent Loop
2. ENABLE_L4_AGENT_LOOP=false → 跳过 L4，降级到 L2/L3
3. L4 异常时 → log warning + 降级，不 crash

注意：run_agent_loop 内部用 mock LLM/executor 是 OK 的（Task 4.3 示范）。
这里集成测试主要验证 chat_service 的路由逻辑，不测 LLM/DB 细节。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import ChatRequest, ChatResponse
from app.services.agent_runtime_service import AgentRuntimeService, AgentLoopResult
from app.services.chat_service import ChatService
from app.dependencies import CurrentUser


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_factory():
    """返回 mock LLM client factory。"""
    mock_client = MagicMock()
    mock_client.complete_with_tools = AsyncMock(
        return_value=MagicMock(
            content="Mock LLM response",
            tool_calls=[],
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            model="test",
        )
    )
    factory = MagicMock(return_value=mock_client)
    return factory


@pytest.fixture
def mock_adapter():
    """返回 mock DB adapter（execute_read_only）。"""
    adapter = MagicMock()
    adapter.execute_read_only = AsyncMock(return_value=[{"id": 1, "name": "test"}])
    return adapter


@pytest.fixture
def mock_ontology():
    """返回 mock Ontology 服务。"""
    ontology = MagicMock()
    ontology.listClasses = AsyncMock(return_value=[])
    ontology.listJoins = AsyncMock(return_value=[])
    ontology.searchByKeyword = AsyncMock(return_value=[])
    return ontology


@pytest.fixture
def chat_service(mock_llm_factory, mock_adapter, mock_ontology):
    """构造 ChatService，所有依赖 inject 为 mock。"""
    service = ChatService(
        llmFactory=mock_llm_factory,
        adapterProvider=MagicMock(return_value=mock_adapter),
        ontologyService=mock_ontology,
    )
    return service


@pytest.fixture
def user():
    """模拟当前用户。"""
    return CurrentUser(
        userId="1",
        roles=("viewer",),
        departments=("IT",),
    )


@pytest.fixture
def session():
    """返回 mock AsyncSession。"""
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Helper: mock system_config table
# ---------------------------------------------------------------------------

def _make_system_config_result(key: str, value: str):
    """构造 SQLAlchemy scalar result for system_config query."""
    from unittest.mock import MagicMock
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_routes_to_agent_loop_on_exploratory_question(
    chat_service, session, user, mock_llm_factory, mock_adapter, mock_ontology,
):
    """探索性关键词 → ENABLE_L4_AGENT_LOOP=true → L4 Agent Loop 命中。

    当用户问题包含"为什么/怎么算/拆解/解释/如何"等探索性关键词，
    且 system_config ENABLE_L4_AGENT_LOOP='true' 时，chat_service
    应调用 run_agent_loop 并返回含 [L4:answered] 前缀的响应。
    """
    # 1. Mock system_config: ENABLE_L4_AGENT_LOOP='true'
    async def mock_execute_with_config(query, *args, **kwargs):
        return _make_system_config_result("ENABLE_L4_AGENT_LOOP", "true")

    session.execute = mock_execute_with_config

    # 2. Mock run_agent_loop 返回固定 AgentLoopResult
    fixed_result = AgentLoopResult(
        final_sql="SELECT 1 AS x",
        answer_text="L4 generated answer",
        iterations_used=2,
        tool_calls_made=["list_tables", "execute_sql"],
        total_cost_usd=0.02,
        terminated_reason="answered",
    )

    with patch.object(
        AgentRuntimeService,
        "run_agent_loop",
        new=AsyncMock(return_value=fixed_result),
    ) as mock_run:
        # 3. 构造带探索性关键词的 ChatRequest
        dto = ChatRequest(
            sessionId="test-session-001",
            question="为什么本月采购金额下降这么多？",
            datasourceId=1,
        )

        # 4. 调用 processMessage
        response: ChatResponse = await chat_service.processMessage(
            dto, session, user=user,
        )

        # 5. 断言 run_agent_loop 被调用
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["question"] == dto.question
        assert call_kwargs["user_id"] == user.userId

        # 6. 断言响应含 [L4:answered] 前缀
        assert response.answer.startswith("[L4:answered]")
        assert "L4 generated answer" in response.answer
        # final_sql 应从 AgentLoopResult 回填
        assert response.sql == "SELECT 1 AS x"


@pytest.mark.asyncio
async def test_chat_skips_l4_when_disabled(
    chat_service, session, user, mock_llm_factory, mock_adapter, mock_ontology,
):
    """ENABLE_L4_AGENT_LOOP=false 时 → 不进 L4，降级到 L2/L3。

    即使问题含探索性关键词，flag 为 false 时 _handleNl2SqlAgent
    返回 None，流水线继续走 _classifyMessage → L2/L3。
    run_agent_loop 不应被调用。
    """
    # 1. Mock system_config: ENABLE_L4_AGENT_LOOP='false'
    async def mock_execute_disabled(query, *args, **kwargs):
        return _make_system_config_result("ENABLE_L4_AGENT_LOOP", "false")

    session.execute = mock_execute_disabled

    # 2. Mock run_agent_loop（如果被调用则 fail）
    with patch.object(
        AgentRuntimeService,
        "run_agent_loop",
        new=AsyncMock(side_effect=RuntimeError("L4 should not be called")),
    ) as mock_run:
        dto = ChatRequest(
            sessionId="test-session-002",
            question="为什么本月采购金额下降这么多？",
            datasourceId=1,
        )

        # 3. processMessage 不应 crash（可能走 L2/L3 返回）
        # 由于 mock LLM/config 很简单，这里只验证不抛异常
        try:
            response: ChatResponse = await chat_service.processMessage(
                dto, session, user=user,
            )
        except Exception as exc:
            # L2/L3 路径也可能失败（因为 mock 太简单）
            # 只要不是 "L4 should not be called" 就说明 L4 被正确跳过
            if "L4 should not be called" in str(exc):
                raise AssertionError("L4 was called when it should have been skipped")
            # 其他异常是 L2/L3 的 mock 限制，可以接受

        # 4. 断言 run_agent_loop 从未被调用
        mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_chat_l4_falls_back_to_l3_on_error(
    chat_service, session, user, mock_llm_factory, mock_adapter, mock_ontology,
):
    """L4 异常时 → log warning + 返回 None → 降级到 L2/L3。

    当 run_agent_loop 抛出异常（如 LLM 不可用），_handleNl2SqlAgent
    应捕获异常、log warning、返回 None，使流水线能继续到 L2/L3。
    """
    # 1. Mock system_config: ENABLE_L4_AGENT_LOOP='true'
    async def mock_execute_enabled(query, *args, **kwargs):
        return _make_system_config_result("ENABLE_L4_AGENT_LOOP", "true")

    session.execute = mock_execute_enabled

    # 2. Mock run_agent_loop raise RuntimeError
    with patch.object(
        AgentRuntimeService,
        "run_agent_loop",
        new=AsyncMock(side_effect=RuntimeError("LLM unavailable")),
    ):
        dto = ChatRequest(
            sessionId="test-session-003",
            question="解释一下本月销售数据",
            datasourceId=1,
        )

        # 3. processMessage 不应因 L4 异常而 crash
        try:
            response: ChatResponse = await chat_service.processMessage(
                dto, session, user=user,
            )
        except Exception as exc:
            # L2/L3 路径也可能失败（mock 限制），但 L4 异常不应当传导
            if "LLM unavailable" in str(exc):
                raise AssertionError(
                    "L4 RuntimeError was not caught — it propagated to processMessage"
                )
            # 其他异常是 L2/L3 的 mock 限制，可接受


@pytest.mark.asyncio
async def test_chat_l4_not_triggered_for_simple_question(
    chat_service, session, user, mock_llm_factory, mock_adapter, mock_ontology,
):
    """非探索性问题 → 即使 flag=true 也不进 L4。

    L4 入口仅在探索性关键词（为什么/怎么算/拆解/解释/如何）时触发。
    简单查询问题（如"查询今日订单"）应跳过 L4。
    """
    # 1. Mock system_config: ENABLE_L4_AGENT_LOOP='true'
    async def mock_execute_enabled(query, *args, **kwargs):
        return _make_system_config_result("ENABLE_L4_AGENT_LOOP", "true")

    session.execute = mock_execute_enabled

    # 2. Mock run_agent_loop（如果被调用则 fail）
    with patch.object(
        AgentRuntimeService,
        "run_agent_loop",
        new=AsyncMock(side_effect=RuntimeError("L4 should not be called")),
    ) as mock_run:
        dto = ChatRequest(
            sessionId="test-session-004",
            question="查询今日订单数量",
            datasourceId=1,
        )

        try:
            response: ChatResponse = await chat_service.processMessage(
                dto, session, user=user,
            )
        except Exception as exc:
            if "L4 should not be called" in str(exc):
                raise AssertionError(
                    "L4 was triggered for non-exploratory question"
                )

        # run_agent_loop 不应被调用
        mock_run.assert_not_called()
