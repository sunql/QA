"""complete_with_tools 的 cachedTokens 字段测试（feat/l4-token-profiling）。

口径：与 LlmResponse.cachedTokens 一致——
- usage.prompt_tokens_details.cached_tokens 存在 → 透传（0 保留 0）；
- 缺失 → None（不伪造 0）；
- usage dict 本身**不加** cached_tokens 键（agent_orchestration_cost.usageFromMapping
  会读 usage.get("cached_tokens")，加键会在编排链路悄悄改变成本口径）。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.openai_client import OpenAiClient

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


def _make_response(
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    cached_tokens: int | None = None,
    has_details: bool = True,
) -> MagicMock:
    raw_message = MagicMock()
    raw_message.content = "ok"
    raw_message.role = "assistant"
    raw_message.tool_calls = []

    raw_usage = MagicMock()
    raw_usage.prompt_tokens = prompt_tokens
    raw_usage.completion_tokens = completion_tokens
    if has_details:
        details = MagicMock()
        details.cached_tokens = cached_tokens
        raw_usage.prompt_tokens_details = details
    else:
        raw_usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = raw_message
    response.model = "deepseek-chat"
    response.usage = raw_usage
    return response


def _make_client(mock_response) -> OpenAiClient:
    config = MagicMock()
    config.model_name = "deepseek-chat"
    config.api_endpoint = None
    client = OpenAiClient(config=config, apiKey="test-key", client=MagicMock())
    client._client = MagicMock()
    client._client.chat.completions.create = AsyncMock(return_value=mock_response)
    return client


async def _complete(client: OpenAiClient):
    return await client.complete_with_tools(
        messages=[LlmMessage(role="user", content="test")],
        tools=TOOLS,
        tool_choice="auto",
    )


async def test_cached_tokens_exposed_when_present():
    result = await _complete(_make_client(_make_response(cached_tokens=1234)))

    assert result.cachedTokens == 1234


async def test_cached_tokens_zero_preserved():
    result = await _complete(_make_client(_make_response(cached_tokens=0)))

    assert result.cachedTokens == 0


async def test_cached_tokens_none_when_details_missing():
    result = await _complete(_make_client(_make_response(has_details=False)))

    assert result.cachedTokens is None


async def test_usage_dict_has_no_cached_tokens_key():
    """usage dict 必须保持三键不变，钉死编排链路零副作用。"""
    result = await _complete(_make_client(_make_response(cached_tokens=1234)))

    assert set(result.usage.keys()) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert "cached_tokens" not in result.usage


async def test_tool_calls_still_parsed_with_cached_tokens():
    mock_response = _make_response(cached_tokens=100)
    raw_tc = MagicMock()
    raw_tc.id = "call_1"
    raw_tc.function = MagicMock()
    raw_tc.function.name = "execute_sql"
    raw_tc.function.arguments = json.dumps({"sql": "SELECT 1"})
    mock_response.choices[0].message.tool_calls = [raw_tc]

    result = await _complete(_make_client(mock_response))

    assert result.cachedTokens == 100
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "execute_sql"
