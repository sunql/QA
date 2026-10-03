"""假设适配层：LLM 输出 → 合法假设列表（fake llmClient）。"""

import pytest

from app.services.research_hypothesis_adapter import generateHypotheses


class _FakeLlm:
    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.calls: list[list] = []

    async def complete(self, messages, **kwargs):  # 与 BaseLlmClient 契约对齐
        self.calls.append(messages)
        return self._raw


@pytest.mark.asyncio
async def test_parses_and_filters_illegal_sql() -> None:
    raw = (
        '```json\n[{"statement": "供应商A供货减少", "driver": "SUPPLIER_NAME", '
        '"verification_sql": "SELECT 1 FROM DUAL"},'
        '{"statement": "坏假设", "driver": null, '
        '"verification_sql": "DELETE FROM T"}]\n```'
    )
    result = await generateHypotheses(_FakeLlm(raw), "为什么下降", "数据摘要", ["SUPPLIER_NAME"])
    assert len(result) == 1
    assert result[0].statement == "供应商A供货减少"
    assert result[0].verificationSql.upper().startswith("SELECT")


@pytest.mark.asyncio
async def test_garbage_output_returns_empty_not_raise() -> None:
    result = await generateHypotheses(_FakeLlm("完全不是 JSON"), "q", "s", [])
    assert result == []
