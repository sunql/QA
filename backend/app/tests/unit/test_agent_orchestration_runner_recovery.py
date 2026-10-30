"""Task 9 unit tests: the persisted-state recovery dispatch.

A crashed worker leaves committed steps behind; the next worker must resume
from them **without** re-planning where the contract says so. Same in-memory
doubles as ``test_agent_orchestration_runner.py`` (see
``app/tests/unit/_agent_orchestration_runner_support.py``):

- a committed PLAN row is reconstructed (CALL_TOOL / FINISH / ASK_USER);
- a committed PLAN whose tool schema drifted must pause instead of looping;
- a RUNNING PLAN row is re-planned on the same row (no second iteration);
- a RUNNING TOOL row is replayed with the original arguments;
- every step builds its own metered factory and drains it.

Crash replay against real PostgreSQL is covered by
``app/tests/integration/test_agent_orchestration_runner_recovery.py``.

共享的内存替身在 ``app/tests/unit/_agent_orchestration_runner_support.py``。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.agent_orchestration_types import (
    CallToolAction,
    FinishAction,
    PlannerProtocolError,
)
from app.tests.unit._agent_orchestration_runner_support import (
    FakePlanner,
    FakeStep,
    FakeStore,
    buildRunner,
    driveRunner,
)

# ---------------------------------------------------------------------------
# persisted-state recovery dispatch
# ---------------------------------------------------------------------------


def seedPlan(store: FakeStore, **overrides: Any) -> FakeStep:
    values: dict[str, Any] = {
        "kind": "PLAN",
        "status": "SUCCEEDED",
        "iteration_no": 1,
    }
    values.update(overrides)
    step = store.appendStep(**values)
    store.run.iteration_count = max(store.run.iteration_count, step.iteration_no)
    return step


@pytest.mark.asyncio
async def test_committed_call_tool_plan_is_reconstructed_without_replanning() -> None:
    runner, store, planner, executor, _ = buildRunner(
        planner=FakePlanner([FinishAction("done")])
    )
    plan = seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "persisted"},
        plan_summary="lookup",
    )

    await driveRunner(runner)

    assert executor.invocations == [{"query": "persisted"}]
    # Only the FINISH round needed the Planner: the action was reconstructed.
    assert len(planner.calls) == 1
    tool = next(step for step in store.steps if step.kind == "TOOL")
    assert tool.tool_name == "wiki_search"
    assert tool.input_json == {"query": "persisted"}
    assert tool.iteration_no == plan.iteration_no
    assert store.run.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_committed_tool_whose_schema_drifted_pauses_instead_of_looping() -> None:
    runner, store, planner, executor, _ = buildRunner(
        planner=FakePlanner(),
        prepareErrors=[
            PlannerProtocolError("tool 'wiki_search' arguments rejected: (query)")
        ],
    )
    plan = seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "persisted"},
        plan_summary="lookup",
    )

    await driveRunner(runner)

    # 旧参数在新的 schema 下永远不可能通过：不得重新调用 Planner 空转。
    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "PLANNER_OUTPUT_INVALID"
    assert plan.status == "SKIPPED"


@pytest.mark.asyncio
async def test_committed_finish_plan_succeeds_without_replanning() -> None:
    runner, store, planner, executor, _ = buildRunner(planner=FakePlanner())
    seedPlan(store, action="FINISH", answer="already planned")

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "already planned"


@pytest.mark.asyncio
async def test_committed_ask_user_plan_pauses_and_is_marked_consumed() -> None:
    runner, store, planner, executor, _ = buildRunner(planner=FakePlanner())
    plan = seedPlan(
        store, action="ASK_USER", answer="which supplier?", plan_summary="ambiguous wording"
    )

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "AMBIGUOUS"
    assert plan.status == "SKIPPED"


@pytest.mark.asyncio
async def test_running_plan_is_reused_by_replanning_without_a_second_iteration() -> None:
    runner, store, planner, executor, _ = buildRunner(
        planner=FakePlanner([FinishAction("done")])
    )
    plan = seedPlan(store, status="RUNNING")
    store.run.iteration_count = 1

    await driveRunner(runner)

    assert len([step for step in store.steps if step.kind == "PLAN"]) == 1
    assert store.steps[0].id == plan.id
    assert store.run.iteration_count == 1
    assert len(planner.calls) == 1


@pytest.mark.asyncio
async def test_running_tool_is_replayed_with_the_original_arguments() -> None:
    runner, store, planner, executor, _ = buildRunner(
        planner=FakePlanner([FinishAction("done")])
    )
    plan = seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "original"},
        plan_summary="lookup",
    )
    tool = store.appendStep(
        kind="TOOL",
        iteration_no=plan.iteration_no,
        tool_name="wiki_search",
        input_json={"query": "original"},
        status="RUNNING",
    )
    store.run.tool_call_count = 1

    await driveRunner(runner)

    assert executor.invocations == [{"query": "original"}]
    assert len([step for step in store.steps if step.kind == "TOOL"]) == 1
    assert store.steps[1].id == tool.id
    assert tool.status == "SUCCEEDED"
    assert store.run.tool_call_count == 1
    assert len(planner.calls) == 1
    assert store.run.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_running_tool_whose_schema_drifted_pauses_and_consumes_the_row() -> None:
    """崩溃重放时工具配置变了：必须暂停（可恢复），不能写终态 FAILED。

    与已提交 PLAN 的同款场景（``test_committed_tool_whose_schema_drifted_...``）
    走同一个暂停码：责任方是配置，不是 Planner。
    """
    runner, store, planner, executor, _ = buildRunner(
        planner=FakePlanner([FinishAction("done")]),
        prepareErrors=[
            PlannerProtocolError("tool 'wiki_search' arguments rejected: limit")
        ],
    )
    plan = seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "original"},
        plan_summary="lookup",
    )
    tool = store.appendStep(
        kind="TOOL",
        iteration_no=plan.iteration_no,
        tool_name="wiki_search",
        input_json={"query": "original"},
        status="RUNNING",
    )
    store.run.tool_call_count = 1

    await driveRunner(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "PLANNER_OUTPUT_INVALID"
    # 不是内部不变量破损：没有写终态错误码/错误文案。
    assert store.run.error_code is None
    assert store.run.error_message is None
    # 过期参数从未被执行。
    assert executor.invocations == []
    # 陈旧的 RUNNING TOOL 行在暂停的同一事务里落到终态：恢复时不会重放它。
    assert tool.status == "SKIPPED"
    assert planner.calls == []


# ---------------------------------------------------------------------------
# metering wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_step_builds_its_own_metered_factory_and_drains_it() -> None:
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "q"}, "lookup"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, factoryClass = buildRunner(planner=planner)

    await driveRunner(runner)

    # Planner usage attaches to the PLAN step; tool-internal calls to the TOOL step.
    assert [item.callerType for item in factoryClass.instances] == [
        "PLANNER",
        "TOOL",
        "PLANNER",
    ]
    assert [item.stepId for item in factoryClass.instances] == [1, 2, 3]
    assert all(item.runId == store.run.id for item in factoryClass.instances)
    assert factoryClass.instances[0].config is not None
    # The tool context carries the exact config and the run's creator.
    toolContext = executor.contexts[0]
    assert toolContext.actor == "tester"
    assert toolContext.llm_config is factoryClass.instances[1].config
    # Every minted client is drained exactly once per step.
    assert [item.drains for item in factoryClass.instances] == [[1], [1], [1]]
