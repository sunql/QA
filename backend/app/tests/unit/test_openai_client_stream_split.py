"""openai_client 流式 usage 与 content 同块双收。

RED: 现实现 usage 块 continue 跳过 content 提取。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.infrastructure.llm.base_client import LlmMessage
from app.infrastructure.llm.openai_client import OpenAiClient


def _make_chunk(*, content: str | None = None, usage: MagicMock | None = None, model: str = "gpt-4"):
    chunk = MagicMock()
    chunk.model = model
    chunk.choices = []
    if content is not None:
        choice = MagicMock()
        choice.delta.content = content
        chunk.choices = [choice]
    chunk.usage = usage
    return chunk


@pytest.mark.asyncio
async def test_stream_usage_and_content_same_chunk_both_yielded():
    """同块既有 usage 又有 content → content 也必须 yield（当前 bug 丢 content）。"""
    usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    chunks = [
        _make_chunk(content="hello"),
        _make_chunk(content="world", usage=usage),  # 同块双收
    ]
    mock_response = AsyncMock()
    mock_response.__aiter__.return_value = iter(chunks)

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    client = OpenAiClient.__new__(OpenAiClient)
    client._client = mock_client
    client._modelName = "gpt-4"
    client._provider = MagicMock()
    client._provider.value = "openai"
    client._disableThinking = True

    out = []
    async for sc in client.completeStream([LlmMessage(role="user", content="hi")]):
        out.append(sc)
    contents = [sc.content for sc in out if sc.content]
    assert "hello" in contents
    assert "world" in contents  # 关键：同块双收时不丢
    # 末块含真实 token
    final = out[-1]
    assert final.isDone is True
    assert final.promptTokens == 10
    assert final.completionTokens == 5
