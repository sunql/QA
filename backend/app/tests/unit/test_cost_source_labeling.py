"""cost_source 写入标注 + 读契约（P1-2 收尾）。

背景：`session_token_usage.cost_source` 列已存在且 chat 主链路写入
`measured`/`estimated`，但（a）研究链路 / 报告总结 / doc-QA 三个写入方不传值
（落 NULL），（b）`SessionTokenUsageRead` 没有该字段 ⇒ 写进台账却永远读不到。

本文件钉住三件事：
1. 读契约以 camelCase 暴露 `costSource`（CamelModel 别名）；
2. 研究链路按名称反查得到配置 ⇒ `measured`；
3. 查不到配置 ⇒ 成本是占位 0，必须标 `unknown` 而不是谎称实测。
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.schemas import SessionTokenUsageRead
from app.services.research_agent_ports import LlmUsageRecorder
from app.services.token_usage_service import (
    COST_SOURCE_MEASURED,
    COST_SOURCE_UNKNOWN,
    TokenUsageService,
)


class _RecordingTokenUsage:
    """记录 recordUsage kwargs 的替身（不触 DB）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def recordUsage(self, session, **kwargs) -> None:
        self.calls.append(kwargs)


class _FakeSession:
    """只实现 `_findConfig` 需要的 `scalar`。"""

    def __init__(self, config) -> None:
        self._config = config

    async def scalar(self, stmt):
        return self._config


def test_session_token_usage_read_exposes_camel_case_cost_source() -> None:
    # Arrange
    row = SessionTokenUsageRead(
        id=1,
        session_id="s-1",
        model_name="fake-mdl",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost=Decimal("0.001"),
        request_time=datetime(2026, 10, 18, tzinfo=UTC),
        purpose="doc_qa",
        cost_source=COST_SOURCE_MEASURED,
    )

    # Act
    payload = row.model_dump(by_alias=True)

    # Assert
    assert payload["costSource"] == COST_SOURCE_MEASURED


def test_session_token_usage_read_keeps_legacy_null() -> None:
    row = SessionTokenUsageRead(
        id=2,
        session_id="s-1",
        model_name=None,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost=Decimal("0"),
        request_time=datetime(2026, 10, 18, tzinfo=UTC),
    )

    assert row.model_dump(by_alias=True)["costSource"] is None


@pytest.mark.asyncio
async def test_record_usage_rejects_unknown_cost_source() -> None:
    # Arrange —— 拼错的口径必须在写库前 fail fast（静默失真无法事后发现）。
    service = TokenUsageService()

    # Act / Assert
    with pytest.raises(ValueError, match="invalid cost_source"):
        await service.recordUsage(
            None,  # 校验先于任何 session 使用
            sessionId="s-1",
            modelConfigId=None,
            modelName=None,
            promptTokens=1,
            completionTokens=1,
            cost=Decimal("0"),
            cost_source="measuredish",
        )


@pytest.mark.asyncio
async def test_llm_usage_recorder_labels_measured_when_config_found() -> None:
    # Arrange
    config = SimpleNamespace(
        id=7,
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    tokenUsage = _RecordingTokenUsage()
    recorder = LlmUsageRecorder(tokenUsage=tokenUsage)

    # Act
    await recorder.recordUsage(
        _FakeSession(config),
        sessionId="s-1",
        purpose="research",
        promptTokens=100,
        completionTokens=50,
        modelName="fake-mdl",
    )

    # Assert
    assert tokenUsage.calls[0]["cost_source"] == COST_SOURCE_MEASURED
    assert tokenUsage.calls[0]["modelConfigId"] == 7


@pytest.mark.asyncio
async def test_llm_usage_recorder_labels_unknown_when_config_missing() -> None:
    # Arrange
    tokenUsage = _RecordingTokenUsage()
    recorder = LlmUsageRecorder(tokenUsage=tokenUsage)

    # Act
    await recorder.recordUsage(
        _FakeSession(None),
        sessionId="s-1",
        purpose="research",
        promptTokens=100,
        completionTokens=50,
        modelName="ghost-model",
    )

    # Assert —— 成本是占位 0，不得标成 measured
    assert tokenUsage.calls[0]["cost_source"] == COST_SOURCE_UNKNOWN
    assert tokenUsage.calls[0]["modelConfigId"] is None
    assert tokenUsage.calls[0]["cost"] == Decimal("0")
