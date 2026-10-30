"""Task 9 integration tests: pause codes, crash-safety and recovery.

These cases share the crash-replay fixtures of
``test_agent_orchestration_runner_recovery.py`` (see
``app/tests/_orchestration_recovery_support.py``) but assert a different
contract: **what happens when a run must stop and later be resumed**.

Covered here:

- the pause code comes from the action's reason, not from a stale step row;
- a crash between "TOOL FAILED committed" and "pause written" reproduces the
  same pause without an extra Planner call;
- a real ``resumeRun`` lets the run continue and re-count the failure budget;
- a replay whose stored tool arguments no longer match the **current** tool
  configuration pauses recoverably instead of writing a terminal FAILED.

共享的替身/fixtures/落库助手在 ``app/tests/_orchestration_recovery_support.py``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dependencies import CurrentUser
from app.domain.agent_orchestration_schemas import AgentRunResume
from app.services.agent_orchestration_run_service import AgentOrchestrationRunService
from app.tests._orchestration_recovery_support import (
    ASK_USER_AMBIGUOUS_JSON,
    CALL_TOOL_JSON,
    FINISH_JSON,
    WORKER_1,
    WORKER_2,
    CrashingStore,
    RecordingExecutor,
    SchemaCheckedExecutor,
    ScriptedLlmClient,
    SimulatedWorkerCrash,
    buildRunner,
    claimRun,
    expireLeaseAndReclaim,
    loadRun,
    loadSteps,
    raiseToolError,
    seedResumePrerequisites,
    seedRun,
    tightenToolSchema,
)
from app.tests._orchestration_recovery_support import (
    scriptedClient as scriptedClient,
)
from app.tests._orchestration_recovery_support import (
    sessionFactory as sessionFactory,
)


async def test_first_execution_ambiguous_reason_pauses_ambiguous(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    """The pause code comes from the ACTION's reason, not the stale step row."""

    client = scriptedClient([ASK_USER_AMBIGUOUS_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)

    await buildRunner(sessionFactory).runClaimed(run.id, WORKER_1)

    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "PAUSED"
    assert persisted.pause_code == "AMBIGUOUS"
    assert persisted.pause_detail is None
    assert len(client.calls) == 1
    steps = await loadSteps(sessionFactory, run.id)
    assert [step.status for step in steps] == ["SKIPPED"]
    assert steps[0].plan_summary == "The request is AMBIGUOUS between two suppliers"


async def test_crash_between_tool_failure_commit_and_pause_does_not_replan(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    """Two committed FAILED rows must reproduce TOOL_FAILED without an LLM call."""

    client = scriptedClient([CALL_TOOL_JSON, CALL_TOOL_JSON])
    run, _ = await seedRun(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    executor = RecordingExecutor(handler=raiseToolError)

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(
            sessionFactory, store=CrashingStore("before_pause"), executor=executor
        ).runClaimed(run.id, WORKER_1)

    crashed = await loadRun(sessionFactory, run.id)
    assert crashed.status == "RUNNING"
    crashedSteps = await loadSteps(sessionFactory, run.id)
    assert [step.kind for step in crashedSteps] == ["PLAN", "TOOL", "PLAN", "TOOL"]
    assert [step.status for step in crashedSteps] == [
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
        "FAILED",
    ]
    assert len(client.calls) == 2
    assert len(executor.invocations) == 2

    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    persisted = await loadRun(sessionFactory, run.id)
    assert persisted.status == "PAUSED"
    assert persisted.pause_code == "TOOL_FAILED"
    # The committed FAILED rows already prove the threshold: no new planning.
    assert len(client.calls) == 2
    steps = await loadSteps(sessionFactory, run.id)
    tools = [step for step in steps if step.kind == "TOOL"]
    assert len(tools) == 2
    # 暂停写入时消费掉失败行：用户恢复后才能重新规划。
    assert [step.status for step in tools] == ["SKIPPED", "SKIPPED"]
    assert len(executor.invocations) == 2


async def test_resume_after_tool_failed_pause_replans_through_the_real_service(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    """Paused TOOL_FAILED → 真实 resumeRun → 重新 claim：必须重新规划并继续。

    第二次执行里第一个工具仍然失败，但这次失败只能算 1 次（不是 3 次），
    所以运行会继续到 FINISH，而不是立刻再次暂停。
    """
    client = scriptedClient(
        [CALL_TOOL_JSON, CALL_TOOL_JSON, CALL_TOOL_JSON, FINISH_JSON]
    )
    run, _ = await seedRun(sessionFactory)
    await seedResumePrerequisites(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)
    executor = RecordingExecutor(handler=raiseToolError)

    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_1)

    paused = await loadRun(sessionFactory, run.id)
    assert paused.status == "PAUSED"
    assert paused.pause_code == "TOOL_FAILED"
    assert len(client.calls) == 2

    # 恢复走真实 Task 7 服务：只改 run 列，步骤行保持暂停时的样子。
    async with sessionFactory() as session:
        await AgentOrchestrationRunService().resumeRun(
            session,
            run.run_id,
            AgentRunResume(),
            CurrentUser(userId="tester"),
        )
    resumed = await loadRun(sessionFactory, run.id)
    assert resumed.status == "QUEUED"
    assert resumed.pause_code is None
    assert resumed.lease_owner is None

    await claimRun(sessionFactory, run.id, WORKER_2)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    finished = await loadRun(sessionFactory, run.id)
    assert finished.status == "SUCCEEDED"
    assert finished.final_answer
    # 两次新的 Planner 调用：恢复后重新规划，一次失败后继续到 FINISH。
    assert len(client.calls) == 4
    assert len(executor.invocations) == 3
    steps = await loadSteps(sessionFactory, run.id)
    tools = [step for step in steps if step.kind == "TOOL"]
    # 暂停时消费的两条旧失败行仍保留审计痕迹，新失败行独立计数。
    assert [step.status for step in tools] == ["SKIPPED", "SKIPPED", "FAILED"]


# ---------------------------------------------------------------------------
# 崩溃重放 × 工具配置变更
# ---------------------------------------------------------------------------

CALL_TOOL_JSON_WITH_LIMIT = (
    '{"action":"CALL_TOOL","toolName":"wiki_search",'
    '"args":{"query":"supplier 1001","limit":5},"summary":"lookup supplier"}'
)


async def _crashWithRunningToolRow(
    sessionFactory: async_sessionmaker[AsyncSession],
    executor: SchemaCheckedExecutor,
) -> tuple[Any, int]:
    """崩溃在 TOOL 行落库之后：留下一个还停在 RUNNING 的陈旧 TOOL 行。"""
    run, _ = await seedRun(sessionFactory)
    await seedResumePrerequisites(sessionFactory)
    await claimRun(sessionFactory, run.id, WORKER_1)

    with pytest.raises(SimulatedWorkerCrash):
        await buildRunner(
            sessionFactory,
            store=CrashingStore("after_get_tool"),
            executor=executor,
        ).runClaimed(run.id, WORKER_1)

    crashed = await loadSteps(sessionFactory, run.id)
    staleTool = next(step for step in crashed if step.kind == "TOOL")
    assert staleTool.status == "RUNNING"
    assert staleTool.input_json == {"query": "supplier 1001"}
    return run, staleTool.id


async def _assertPausedOnSchemaDrift(
    sessionFactory: async_sessionmaker[AsyncSession],
    run: Any,
    staleToolId: int,
    executor: SchemaCheckedExecutor,
) -> None:
    """重放遇到过期参数：暂停（可恢复），陈旧的 RUNNING 行被消费掉。"""
    await expireLeaseAndReclaim(sessionFactory, run.id)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    paused = await loadRun(sessionFactory, run.id)
    assert paused.status == "PAUSED"
    assert paused.pause_code == "PLANNER_OUTPUT_INVALID"
    # 不是内部不变量破损：没有写终态错误码/错误文案。
    assert paused.error_code is None
    assert paused.error_message is None
    # 过期参数从未被交给工具处理器。
    assert executor.invocations == []

    steps = await loadSteps(sessionFactory, run.id)
    stale = next(step for step in steps if step.id == staleToolId)
    # 陈旧的 RUNNING TOOL 行在暂停的同一事务里落到终态：恢复时不会重放它。
    assert stale.status == "SKIPPED"
    assert stale.output_json is None


async def _resumeAndFinish(
    sessionFactory: async_sessionmaker[AsyncSession],
    run: Any,
    executor: SchemaCheckedExecutor,
) -> None:
    """恢复走真实服务：新参数被执行，运行成功结束。"""
    async with sessionFactory() as session:
        await AgentOrchestrationRunService().resumeRun(
            session,
            run.run_id,
            AgentRunResume(),
            CurrentUser(userId="tester"),
        )
    resumed = await loadRun(sessionFactory, run.id)
    assert resumed.status == "QUEUED"
    assert resumed.pause_code is None

    await claimRun(sessionFactory, run.id, WORKER_2)
    await buildRunner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    finished = await loadRun(sessionFactory, run.id)
    assert finished.status == "SUCCEEDED"
    assert finished.pause_code is None
    # 旧参数从未被执行；恢复后的工具调用用的是符合当前 schema 的参数。
    assert executor.invocations == [{"query": "supplier 1001", "limit": 5}]
    finalSteps = await loadSteps(sessionFactory, run.id)
    assert [step.status for step in finalSteps if step.kind == "TOOL"] == [
        "SKIPPED",
        "SUCCEEDED",
    ]


async def test_replay_after_tool_schema_drift_pauses_and_stays_recoverable(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], ScriptedLlmClient],
) -> None:
    """崩溃重放时工具配置已收紧：必须**暂停**（可恢复），不能写终态 FAILED。

    落库的旧参数（缺 limit）不再匹配当前 schema ⇒ 执行器抛
    ``PlannerProtocolError``。运行必须停在 PAUSED（用户可恢复），而不是
    FAILED（终态、不可恢复，且错误文案会把配置变更误报成内部不变量破损）。
    """
    client = scriptedClient(
        [CALL_TOOL_JSON, CALL_TOOL_JSON_WITH_LIMIT, FINISH_JSON]
    )
    executor = SchemaCheckedExecutor()

    run, staleToolId = await _crashWithRunningToolRow(sessionFactory, executor)
    # 管理员收紧了工具 schema：落库参数不再匹配**当前**配置。
    await tightenToolSchema(sessionFactory)

    await _assertPausedOnSchemaDrift(sessionFactory, run, staleToolId, executor)
    await _resumeAndFinish(sessionFactory, run, executor)

    # 三次 Planner 调用：崩溃前一次、恢复后重新规划两次（工具调用 + 收尾）。
    assert len(client.calls) == 3
    # 过期参数从未作为**观测**进入下一次 Planner 提示：陈旧行没有 output_json，
    # 因此渲染历史里没有任何 TOOL 块（与 ACL 拒绝路径同一不变量）。
    secondPrompt = "\n".join(message.content for message in client.calls[1])
    assert "Step 1 (TOOL" not in secondPrompt
