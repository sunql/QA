"""Task 6 integration tests: per-call usage rows against real PostgreSQL.

Every test drives the real recorder (its own short session, real row locks)
and then reads the persisted rows back: one usage row per LLM call, the run
and step counters folded in the same transaction, and the run's ``cost_source``
recomputed from all usage rows of that run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
    AgentOrchestrationUsage,
)
from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig
from app.infrastructure import database as dbModule
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse, StreamChunk
from app.services.agent_orchestration_metering import (
    MeteredLlmClient,
    OrchestrationUsageRecorder,
)
from app.services.chat_usage import UsageMixin


class _FakeLlmClient:
    """Provider double: one deterministic answer, no network."""

    def __init__(
        self,
        error: Exception | None = None,
        promptTokens: int = 11,
        completionTokens: int = 7,
        chunks: list[StreamChunk] | None = None,
    ) -> None:
        self.error = error
        self.chunks = chunks
        self.response = LlmResponse(
            content="ok",
            modelName="orchestration-metering-model",
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            totalTokens=promptTokens + completionTokens,
            cachedTokens=3,
        )

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        if self.error is not None:
            raise self.error
        return self.response

    async def complete_with_tools(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def completeStream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for chunk in self.chunks or []:
            yield chunk

    async def close(self) -> None:
        return None


async def _seedConfig(dbSession: AsyncSession) -> LlmConfig:
    config = LlmConfig(
        model_name="orchestration-metering-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


async def _seedRun(dbSession: AsyncSession, config: LlmConfig) -> AgentOrchestrationRun:
    run = AgentOrchestrationRun(
        run_id="11111111-2222-3333-4444-555555555555",
        agent_code="metering-agent",
        profile_revision=1,
        instructions_snapshot="instructions",
        tool_bindings_snapshot=[],
        max_observation_chars=1000,
        question="how many suppliers?",
        created_by="tester",
        model_config_id=config.id,
        status="RUNNING",
        max_iterations=5,
        max_tool_calls=5,
        max_cost_usd=Decimal("1"),
        timeout_seconds=60,
    )
    dbSession.add(run)
    await dbSession.commit()
    await dbSession.refresh(run)
    return run


async def _seedStep(dbSession: AsyncSession, run: AgentOrchestrationRun) -> AgentOrchestrationStep:
    step = AgentOrchestrationStep(
        run_id=run.id,
        step_no=0,
        iteration_no=0,
        kind="PLAN",
        status="RUNNING",
    )
    dbSession.add(step)
    await dbSession.commit()
    await dbSession.refresh(step)
    return step


async def _usageRows(dbSession: AsyncSession, runId: int) -> list[AgentOrchestrationUsage]:
    rows = (
        (
            await dbSession.execute(
                select(AgentOrchestrationUsage)
                .where(AgentOrchestrationUsage.run_id == runId)
                .order_by(AgentOrchestrationUsage.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _reloadRun(dbSession: AsyncSession, runId: int) -> AgentOrchestrationRun:
    run = await dbSession.get(AgentOrchestrationRun, runId)
    assert run is not None
    await dbSession.refresh(run)
    return run


async def _reloadStep(dbSession: AsyncSession, stepId: int) -> AgentOrchestrationStep:
    step = await dbSession.get(AgentOrchestrationStep, stepId)
    assert step is not None
    await dbSession.refresh(step)
    return step


def _recorder(config: LlmConfig) -> OrchestrationUsageRecorder:
    return OrchestrationUsageRecorder(dbModule.getSessionFactory(), config)


async def test_successful_call_writes_one_usage_row_and_updates_counters(
    dbSession: AsyncSession,
) -> None:
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    step = await _seedStep(dbSession, run)
    recorder = _recorder(config)
    client = MeteredLlmClient(
        _FakeLlmClient(), recorder, runId=run.id, stepId=step.id, callerType="PLANNER"
    )

    await client.complete([LlmMessage(role="user", content="hi")])

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 1
    usage = rows[0]
    assert usage.run_id == run.id
    assert usage.step_id == step.id
    assert usage.caller_type == "PLANNER"
    assert usage.model_config_id == config.id
    assert usage.model_name == "orchestration-metering-model"
    assert usage.prompt_tokens == 11
    assert usage.completion_tokens == 7
    assert usage.cached_tokens == 3
    assert usage.cost_source == "ESTIMATED"
    assert usage.call_status == "SUCCEEDED"

    expectedCost = UsageMixin.calculateCost(
        config, 11, 7, cachedTokens=3, cacheHitMultiplier=0.0
    )
    assert usage.cost == expectedCost

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.prompt_tokens == 11
    assert reloadedRun.completion_tokens == 7
    assert reloadedRun.total_tokens == 18
    assert reloadedRun.cost == expectedCost
    assert reloadedRun.cost_source == "ESTIMATED"
    assert reloadedRun.cost_complete is True

    reloadedStep = await _reloadStep(dbSession, step.id)
    assert reloadedStep.prompt_tokens == 11
    assert reloadedStep.completion_tokens == 7
    assert reloadedStep.cost == expectedCost


async def test_failed_call_without_usage_is_recorded_as_unknown(
    dbSession: AsyncSession,
) -> None:
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    recorder = _recorder(config)
    client = MeteredLlmClient(
        _FakeLlmClient(error=LlmClientError("provider down")),
        recorder,
        runId=run.id,
        stepId=None,
        callerType="TOOL",
    )

    with pytest.raises(LlmClientError):
        await client.complete([LlmMessage(role="user", content="hi")])

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 1
    assert rows[0].call_status == "FAILED"
    assert rows[0].cost_source == "UNKNOWN"
    assert rows[0].prompt_tokens == 0
    assert rows[0].completion_tokens == 0
    assert rows[0].step_id is None
    assert rows[0].caller_type == "TOOL"

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.cost_source == "UNKNOWN"
    assert reloadedRun.cost_complete is False
    assert reloadedRun.total_tokens == 0


async def test_planner_output_invalid_updates_the_same_usage_row(
    dbSession: AsyncSession,
) -> None:
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    step = await _seedStep(dbSession, run)
    recorder = _recorder(config)
    client = MeteredLlmClient(
        _FakeLlmClient(), recorder, runId=run.id, stepId=step.id, callerType="PLANNER"
    )

    completion = await client.completeForPlanning(
        [LlmMessage(role="user", content="hi")]
    )
    await client.markOutputInvalid(completion.usageId)

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 1
    usage = rows[0]
    assert usage.id == completion.usageId
    assert usage.call_status == "OUTPUT_INVALID"
    # Token counts must survive the status flip untouched.
    assert usage.prompt_tokens == 11
    assert usage.completion_tokens == 7
    assert usage.cost_source == "ESTIMATED"

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.total_tokens == 18
    assert reloadedRun.cost_source == "ESTIMATED"
    assert reloadedRun.cost_complete is True


async def test_mixed_cost_sources_are_aggregated_on_the_run(
    dbSession: AsyncSession,
) -> None:
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    recorder = _recorder(config)

    await recorder.recordUsage(
        runId=run.id,
        stepId=None,
        callerType="PLANNER",
        modelConfigId=config.id,
        modelName=config.model_name,
        promptTokens=10,
        completionTokens=5,
        cachedTokens=None,
        cost=Decimal("0.1"),
        costSource="MEASURED",
        callStatus="SUCCEEDED",
    )

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.cost_source == "MEASURED"
    assert reloadedRun.cost_complete is True

    await recorder.recordUsage(
        runId=run.id,
        stepId=None,
        callerType="PLANNER",
        modelConfigId=config.id,
        modelName=config.model_name,
        promptTokens=4,
        completionTokens=2,
        cachedTokens=None,
        cost=Decimal("0.2"),
        costSource="ESTIMATED",
        callStatus="SUCCEEDED",
    )

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.cost_source == "MIXED"
    assert reloadedRun.cost_complete is True
    assert reloadedRun.prompt_tokens == 14
    assert reloadedRun.completion_tokens == 7
    assert reloadedRun.total_tokens == 21
    assert reloadedRun.cost == Decimal("0.3")


async def test_unknown_usage_row_makes_the_run_incomplete(
    dbSession: AsyncSession,
) -> None:
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    recorder = _recorder(config)

    await recorder.recordUsage(
        runId=run.id,
        stepId=None,
        callerType="PLANNER",
        modelConfigId=config.id,
        modelName=config.model_name,
        promptTokens=10,
        completionTokens=5,
        cachedTokens=None,
        cost=Decimal("0.1"),
        costSource="ESTIMATED",
        callStatus="SUCCEEDED",
    )
    await recorder.recordUsage(
        runId=run.id,
        stepId=None,
        callerType="PLANNER",
        modelConfigId=config.id,
        modelName=config.model_name,
        promptTokens=0,
        completionTokens=0,
        cachedTokens=None,
        cost=Decimal("0"),
        costSource="UNKNOWN",
        callStatus="FAILED",
    )

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.cost_source == "UNKNOWN"
    assert reloadedRun.cost_complete is False


async def test_two_calls_accumulate_on_run_and_step(
    dbSession: AsyncSession,
) -> None:
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    step = await _seedStep(dbSession, run)
    recorder = _recorder(config)
    client = MeteredLlmClient(
        _FakeLlmClient(), recorder, runId=run.id, stepId=step.id, callerType="TOOL"
    )

    await client.complete([LlmMessage(role="user", content="one")])
    await client.complete([LlmMessage(role="user", content="two")])

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 2
    assert len({row.id for row in rows}) == 2

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.prompt_tokens == 22
    assert reloadedRun.completion_tokens == 14
    assert reloadedRun.total_tokens == 36

    reloadedStep = await _reloadStep(dbSession, step.id)
    assert reloadedStep.prompt_tokens == 22
    assert reloadedStep.completion_tokens == 14


async def test_concurrent_recorders_do_not_deadlock_or_lose_counters(
    dbSession: AsyncSession,
) -> None:
    """Four recorders on one run must all succeed and all be accounted.

    The recorder locks the run row *before* inserting the usage row. If the
    insert came first, every recorder would hold a key-share lock on the run
    row (foreign key check) and then wait for the update lock — a genuine
    PostgreSQL deadlock between the recorders.
    """
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    recorder = _recorder(config)

    async def record(index: int) -> None:
        await recorder.recordUsage(
            runId=run.id,
            stepId=None,
            callerType="PLANNER",
            modelConfigId=config.id,
            modelName=config.model_name,
            promptTokens=index + 1,
            completionTokens=1,
            cachedTokens=None,
            cost=Decimal("0.01"),
            costSource="ESTIMATED",
            callStatus="SUCCEEDED",
        )

    await asyncio.gather(*[record(index) for index in range(4)])

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 4
    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.prompt_tokens == 1 + 2 + 3 + 4
    assert reloadedRun.completion_tokens == 4
    assert reloadedRun.total_tokens == 14
    assert reloadedRun.cost == Decimal("0.04")
    assert reloadedRun.cost_source == "ESTIMATED"
    assert reloadedRun.cost_complete is True


async def test_recorder_failure_is_raised_not_swallowed(
    dbSession: AsyncSession,
) -> None:
    """A missing run must surface as an error, never as a silent drop."""
    config = await _seedConfig(dbSession)
    recorder = _recorder(config)

    with pytest.raises((IntegrityError, ValueError)):
        await recorder.recordUsage(
            runId=999999,
            stepId=None,
            callerType="PLANNER",
            modelConfigId=config.id,
            modelName=config.model_name,
            promptTokens=1,
            completionTokens=1,
            cachedTokens=None,
            cost=Decimal("0.1"),
            costSource="ESTIMATED",
            callStatus="SUCCEEDED",
        )

    rows = await _usageRows(dbSession, 999999)
    assert rows == []


async def test_usage_less_call_is_recorded_as_unknown_and_leaves_the_run_incomplete(
    dbSession: AsyncSession,
) -> None:
    """provider 不报用量 ⇒ UNKNOWN + cost_complete=false。

    记成 ESTIMATED/cost 0 会让 run 变成「已知的完整账单」，而账单数字是编的。
    """
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    recorder = _recorder(config)
    client = MeteredLlmClient(
        _FakeLlmClient(promptTokens=0, completionTokens=0),
        recorder,
        runId=run.id,
        stepId=None,
        callerType="PLANNER",
    )

    await client.complete([LlmMessage(role="user", content="hi")])

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 1
    usage = rows[0]
    assert usage.call_status == "SUCCEEDED"
    assert usage.cost_source == "UNKNOWN"
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.cost == Decimal("0")

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.cost_source == "UNKNOWN"
    assert reloadedRun.cost_complete is False
    assert reloadedRun.total_tokens == 0
    assert reloadedRun.cost == Decimal("0")


async def test_stream_without_terminal_usage_is_recorded_as_unknown(
    dbSession: AsyncSession,
) -> None:
    """ollama 截断流：终止块 0/0 ⇒ 用量未知 ⇒ run 不可声称账单完整。"""
    config = await _seedConfig(dbSession)
    run = await _seedRun(dbSession, config)
    recorder = _recorder(config)
    client = MeteredLlmClient(
        _FakeLlmClient(
            chunks=[
                StreamChunk(
                    content="a",
                    isDone=False,
                    promptTokens=0,
                    completionTokens=0,
                    modelName="orchestration-metering-model",
                ),
                StreamChunk(
                    content="",
                    isDone=True,
                    promptTokens=0,
                    completionTokens=0,
                    modelName="orchestration-metering-model",
                ),
            ]
        ),
        recorder,
        runId=run.id,
        stepId=None,
        callerType="PLANNER",
    )

    chunks = [
        chunk
        async for chunk in client.completeStream([LlmMessage(role="user", content="hi")])
    ]
    assert len(chunks) == 2

    rows = await _usageRows(dbSession, run.id)
    assert len(rows) == 1
    usage = rows[0]
    assert usage.call_status == "SUCCEEDED"
    assert usage.cost_source == "UNKNOWN"
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0

    reloadedRun = await _reloadRun(dbSession, run.id)
    assert reloadedRun.cost_source == "UNKNOWN"
    assert reloadedRun.cost_complete is False
