"""Tool calling support tests for BaseLlmClient / OpenAiClient."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage
from app.infrastructure.llm.openai_client import OpenAiClient


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #

def _make_mock_openai_response(
    content: str | None,
    tool_calls: list[dict] | None = None,
    model: str = "gpt-4",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> MagicMock:
    """Build a mock openai.ChatCompletion chunk with the shape we need."""
    raw_message = MagicMock()
    raw_message.content = content
    raw_message.role = "assistant"
    raw_message.tool_calls = []
    if tool_calls:
        for tc in tool_calls:
            raw_tc = MagicMock()
            raw_tc.id = tc["id"]
            raw_tc.function = MagicMock()
            raw_tc.function.name = tc["function"]["name"]
            raw_tc.function.arguments = json.dumps(tc["function"]["arguments"])
            raw_message.tool_calls.append(raw_tc)

    raw_usage = MagicMock()
    raw_usage.prompt_tokens = prompt_tokens
    raw_usage.completion_tokens = completion_tokens

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = raw_message
    response.model = model
    response.usage = raw_usage
    return response


def _make_config(model_name: str = "gpt-4", api_endpoint: str | None = None) -> MagicMock:
    cfg = MagicMock()
    cfg.model_name = model_name
    cfg.api_endpoint = api_endpoint
    return cfg


# --------------------------------------------------------------------------- #
# Tests – BaseLlmClient abstract-method presence
# --------------------------------------------------------------------------- #

def test_base_llm_client_has_complete_with_tools_abstract():
    """BaseLlmClient must declare complete_with_tools as abstract."""

    class MinimalClient(BaseLlmClient):
        async def complete(self, messages: list[LlmMessage], **kwargs: Any):
            raise NotImplementedError

        def completeStream(self, messages: list[LlmMessage], **kwargs: Any):
            raise NotImplementedError

        async def close(self) -> None:
            raise NotImplementedError

        async def complete_with_tools(
            self,
            messages: list[LlmMessage],
            tools: list[dict] | None = None,
            tool_choice: str | dict = "auto",
        ):
            raise NotImplementedError

    # Should not raise TypeError about abstract methods
    client: BaseLlmClient = MinimalClient()
    assert hasattr(client, "complete_with_tools")


# --------------------------------------------------------------------------- #
# Tests – OpenAiClient.complete_with_tools
# --------------------------------------------------------------------------- #

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": "执行 SQL 查询",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
    }
]


@pytest.mark.asyncio
async def test_complete_with_tools_returns_tool_calls():
    """complete_with_tools parses OpenAI tool_calls into ToolCall list."""
    mock_response = _make_mock_openai_response(
        content=None,
        tool_calls=[
            {
                "id": "call_abc123",
                "function": {"name": "execute_sql", "arguments": {"sql": "SELECT 1"}},
            }
        ],
    )

    config = _make_config()
    client = OpenAiClient(config=config, apiKey="test-key", client=MagicMock())
    client._client = MagicMock()
    client._client.chat.completions.create = AsyncMock(return_value=mock_response)

    result = await client.complete_with_tools(
        messages=[LlmMessage(role="user", content="查 PO 完成率")],
        tools=TOOLS,
    )

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_abc123"
    assert result.tool_calls[0].name == "execute_sql"
    assert result.tool_calls[0].args == {"sql": "SELECT 1"}


@pytest.mark.asyncio
async def test_complete_with_tools_falls_back_to_content():
    """When OpenAI returns no tool_calls, content is returned and tool_calls is empty."""
    mock_response = _make_mock_openai_response(
        content="以下是查询结果：42",
        tool_calls=None,
    )

    config = _make_config()
    client = OpenAiClient(config=config, apiKey="test-key", client=MagicMock())
    client._client = MagicMock()
    client._client.chat.completions.create = AsyncMock(return_value=mock_response)

    result = await client.complete_with_tools(
        messages=[LlmMessage(role="user", content="查 PO 完成率")],
        tools=[],
    )

    assert result.content == "以下是查询结果：42"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_complete_with_tools_passes_tools_and_tool_choice():
    """tools list and tool_choice are forwarded to the OpenAI API."""
    config = _make_config()
    client = OpenAiClient(config=config, apiKey="test-key", client=MagicMock())
    mock_create = AsyncMock(
        return_value=_make_mock_openai_response(content="ok", tool_calls=None)
    )
    client._client = MagicMock()
    client._client.chat.completions.create = mock_create

    await client.complete_with_tools(
        messages=[LlmMessage(role="user", content="test")],
        tools=TOOLS,
        tool_choice="auto",
    )

    mock_create.assert_awaited_once()
    _, kwargs = mock_create.call_args
    assert kwargs["tools"] == TOOLS
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_complete_with_tools_returns_usage_and_model():
    """Response includes usage dict and model string."""
    mock_response = _make_mock_openai_response(
        content="result",
        tool_calls=None,
        model="gpt-4o",
        prompt_tokens=5,
        completion_tokens=15,
    )

    config = _make_config()
    client = OpenAiClient(config=config, apiKey="test-key", client=MagicMock())
    client._client = MagicMock()
    client._client.chat.completions.create = AsyncMock(return_value=mock_response)

    result = await client.complete_with_tools(
        messages=[LlmMessage(role="user", content="test")],
        tools=[],
    )

    assert result.model == "gpt-4o"
    assert result.usage is not None
    assert result.usage["prompt_tokens"] == 5
    assert result.usage["completion_tokens"] == 15
