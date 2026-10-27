"""Task 9 integration tests: crash replay against real PostgreSQL.

The runner, the Planner, the real store and the real metering recorder all run
against PostgreSQL. Only two edges are doubled:

- ``createClient`` (the provider boundary) returns a scripted
  ``BaseLlmClient`` so the Planner receives deterministic protocol JSON while
  every usage row is still written by the real meter;
- the tool executor is a recorder, because the crash matrix is about step
  replay, not about the two-phase executor (covered by Task 4 tests).

A crash is a ``BaseException`` raised from inside a committed or uncommitted
store transition. The worker (Task 8) only catches ``Exception``, so this
reproduces a process dying between two durable writes. The test then expires
the lease, re-claims the run as a second worker and asserts the recovery
contract from the design (§8): the same TOOL row/arguments are reused, the
handler may run twice but only one TOOL row exists, prior PLAN + usage rows
survive, and a committed PLAN is never re-planned.

共享的替身/fixtures/落库助手在 ``app/tests/_orchestration_recovery_support.py``；
暂停码与崩溃安全回归在 ``test_agent_orchestration_runner_recovery_pause.py``。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_models import AgentOrchestrationRun
from app.tests._orchestration_recovery_support import (
    ASK_USER_JSON,
    CALL_TOOL_JSON,
    FINISH_JSON,
    WORKER_1,
    WORKER_2,
    CrashingStore,
    RecordingExecutor,
    ScriptedLlmClient,
    SimulatedWorkerCrash,
    buildRunner,
    claimRun,
    expireLeaseAndReclaim,
    loadRun,
    loadSteps,
    loadUsage,
    seedRun,
)
from app.tests._orchestration_recovery_support import (
    scriptedClient as scriptedClient,
)
from app.tests._orchestration_recovery_support import (
    sessionFactory as sessionFactory,
)


async def _crashAfterToolOutputCommits(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> tuple[Any, RecordingExecutor, list[Any]]:
    """跑到「TOOL 观测已提交」后崩溃，返回 (run, executor, 崩溃后 usage 行)。"""
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    executor = RecordingExecutor()
    crasher = CrashingStore("after_complete_tool")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    crashedSteps = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in crashedSteps] == ["PLAN", "TOOL"]
    assert crashedSteps[1].status == "SUCCEEDED"
    assert crashedSteps[1].output_json is not None
    assert crashedSteps[1].output_json["text"]
    assert len(executor.invocations) == 1
    usageAfterCrash = await loadUsage(sessionFactory, run.id)
    assert len(usageAfterCrash) == 1
    assert usageAfterCrash[0].step_id == crashedSteps[0].id
    return run, executor, usageAfterCrash


def _committedToolObservationChecker(
    sessionFactory: async_sessionmaker[AsyncSession],
    runId: int,
) -> Callable[[int], None]:
    """第 2 次 Planner 调用前，确认已提交的 TOOL 观测还在（不被重放覆盖）。"""

    async def _check(callIndex: int) -> None:
        if callIndex != 2:
            return
        steps = await loadSteps(sessionFactory, runId)
        assert steps[1].kind == "TOOL"
        assert steps[1].status == "SUCCEEDED"
        assert steps[1].output_json is not None
        assert "one supplier" in steps[1].output_json["text"]

    return _check


async def test_crash_after_tool_output_commits_continues_from_the_observation(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    client = scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, executor, usageAfterCrash = await _crashAfterToolOutputCommits(sessionFactory)
    planUsage = (usageAfterCrash[0].prompt_tokens, usageAfterCrash[0].completion_tokens)

    client.onCall = _committedToolObservationChecker(sessionFactory, run.id)

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    steps = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in steps] == ["PLAN", "TOOL", "PLAN"]
    assert len([step for step in steps if step.kind == "TOOL"]) == 1
    assert len(executor.invocations) == 1
    assert executor.invocations == [{"query": "supplier 1001"}]
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.final_answer == "supplier 1001 is owned by procurement"
    usage = await loadUsage(sessionFactory, run.id)
    assert len(usage) == 2
    assert usage[0].id == usageAfterCrash[0].id
    assert (usage[0].prompt_tokens, usage[0].completion_tokens) == planUsage
    assert usage[1].step_id == steps[2].id


async def test_crash_after_tool_row_creation_replays_the_same_step_and_args(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    executor = RecordingExecutor()
    crasher = CrashingStore("after_get_tool")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    afterCrash = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN", "TOOL"]
    assert afterCrash[1].status == "RUNNING"
    toolStepId = afterCrash[1].id
    assert afterCrash[1].input_json == {"query": "supplier 1001"}
    assert executor.invocations == []

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    steps = await loadSteps(sessionFactory, run.id)
    tools = [step for step in steps if step.kind == "TOOL"]
    assert len(tools) == 1
    assert tools[0].id == toolStepId
    assert tools[0].status == "SUCCEEDED"
    assert executor.invocations == [{"query": "supplier 1001"}]
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.tool_call_count == 1


async def test_crash_after_handler_before_output_commit_replays_the_handler(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    executor = RecordingExecutor()
    crasher = CrashingStore("before_complete_tool")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    stepsAfterCrash = await loadSteps(sessionFactory, run.id)
    toolStep = next(step for step in stepsAfterCrash if step.kind == "TOOL")
    assert toolStep.status == "RUNNING"
    assert toolStep.output_json is None
    assert toolStep.step_no == 1
    firstArgs = list(executor.invocations)

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    assert len(executor.invocations) == 2
    assert executor.invocations == [firstArgs[0], firstArgs[0]]
    steps = await loadSteps(sessionFactory, run.id)
    assert len([step for step in steps if step.kind == "TOOL"]) == 1
    assert steps[1].id == toolStep.id
    assert steps[1].status == "SUCCEEDED"
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.tool_call_count == 1


async def test_crash_during_running_plan_reuses_the_row_and_one_iteration(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    client = scriptedClient([FINISH_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    crasher = CrashingStore("after_get_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher).runClaimed(run.id, WORKER_1)

    afterCrash = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN"]
    assert afterCrash[0].status == "RUNNING"
    planStepId = afterCrash[0].id
    crashedRun = await loadRun(sessionFactory, run.id)
    assert crashedRun.iteration_count == 1
    assert client.calls == []

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory).runClaimed(run.id, WORKER_2)

    steps = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in steps] == ["PLAN"]
    assert steps[0].id == planStepId
    assert steps[0].status == "SUCCEEDED"
    assert steps[0].action == "FINISH"
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.iteration_count == 1
    assert persisted.status == "SUCCEEDED"
    assert len(client.calls) == 1


async def test_crash_after_call_tool_plan_commit_reconstructs_without_a_planner_call(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    client = scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    executor = RecordingExecutor()
    crasher = CrashingStore("after_complete_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    afterCrash = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN"]
    assert afterCrash[0].status == "SUCCEEDED"
    assert afterCrash[0].action == "CALL_TOOL"
    assert len(client.calls) == 1
    assert executor.invocations == []

    async def _assertNoReplanBeforeTool(callIndex: int) -> None:
        if callIndex != 2:
            return
        assert executor.invocations == [{"query": "supplier 1001"}]
        steps = await loadSteps(sessionFactory, run.id)
        assert len([step for step in steps if step.kind == "TOOL"]) == 1

    client.onCall = _assertNoReplanBeforeTool

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    assert len(client.calls) == 2
    steps = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in steps] == ["PLAN", "TOOL", "PLAN"]
    assert steps[1].input_json == {"query": "supplier 1001"}
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"


async def test_crash_after_finish_plan_commit_succeeds_without_replanning(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    client = scriptedClient([FINISH_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    crasher = CrashingStore("after_complete_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher).runClaimed(run.id, WORKER_1)

    afterCrash = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN"]
    assert afterCrash[0].status == "SUCCEEDED"
    assert afterCrash[0].action == "FINISH"

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory).runClaimed(run.id, WORKER_2)

    assert len(client.calls) == 1
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.final_answer == "supplier 1001 is owned by procurement"


async def test_crash_after_ask_user_plan_commit_pauses_without_replanning(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    client = scriptedClient([ASK_USER_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    crasher = CrashingStore("after_complete_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(sessionFactory, store=crasher).runClaimed(run.id, WORKER_1)

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory).runClaimed(run.id, WORKER_2)

    assert len(client.calls) == 1
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "PAUSED"
    assert persisted.pause_code == "NEEDS_INPUT"
    steps = await loadSteps(sessionFactory, run.id)
    assert [step.status for step in steps] == ["SKIPPED"]


async def test_reclaimed_run_with_cancellation_finalizes_without_any_llm_call(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    """A RUNNING run cancelled while its worker was dead must not resume work."""

    client = scriptedClient([FINISH_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)

    now = datetime.now(UTC)
    async with sessionFactory() as session:
        await session.execute(
            update(AgentOrchestrationRun)
            .where(AgentOrchestrationRun.id == run.id)
            .values(
                cancel_requested=True,
                lease_until=now - timedelta(seconds=5),
            )
        )
        await session.commit()

    await claimRun(sessionFactory, run.id, WORKER_2)
    await buildRunner(sessionFactory).runClaimed(run.id, WORKER_2)

    assert client.calls == []
    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "CANCELLED"
    assert persisted.lease_owner is None
    assert persisted.finished_at is not None
    steps = await loadSteps(sessionFactory, run.id)
    assert steps == []

