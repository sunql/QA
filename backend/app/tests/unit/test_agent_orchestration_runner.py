"""Task 9 unit tests: the runner's core state machine.

The runner is driven by an in-memory store/planner/executor/metering fake: no
database and no provider are involved. The fake store implements the same
conditional-transition contract as ``AgentOrchestrationStore`` (RUNNING lease,
plan/tool allocation, completion, terminal transitions) so the assertions pin
the *runner's* decisions: which step is created when, which call is withheld by
a budget, and how every terminal transition is persisted.

This module covers the core loop: FINISH, ASK_USER, tool ordering, Planner
protocol corrections, permission denial and the three budgets. Tool failures,
infrastructure errors and lease/cancel handling live in
``test_agent_orchestration_runner_failures.py``; the persisted-state recovery
dispatch lives in ``test_agent_orchestration_runner_recovery.py``.

共享的内存替身在 ``app/tests/unit/_agent_orchestration_runner_support.py``；
真实 PostgreSQL 上的崩溃恢复在
``app/tests/integration/test_agent_orchestration_runner_recovery.py``。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.services.agent_orchestration_types import (
    AskUserAction,
    CallToolAction,
    FinishAction,
    OrchestrationPauseError,
    PlannerProtocolError,
)
from app.tests.unit._agent_orchestration_runner_support import (
    MAX_CORRECTIONS,
    NOW,
    FakePlanner,
    buildRunner,
    driveRunner,
    makeRun,
)

# ---------------------------------------------------------------------------
# 1. FINISH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_writes_the_answer_on_plan_and_run() -> None:
    planner = FakePlanner([FinishAction("供应商 1001 由采购部负责")])
    runner, store, planner, executor, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    plan = store.steps[0]
    assert plan.kind == "PLAN"
    assert plan.action == "FINISH"
    assert plan.answer == "供应商 1001 由采购部负责"
    assert plan.status == "SUCCEEDED"
    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "供应商 1001 由采购部负责"
    assert store.run.finished_at == NOW
    assert executor.invocations == []
    assert len(planner.calls) == 1


# ---------------------------------------------------------------------------
# 2. ASK_USER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_pauses_needs_input_without_a_tool_rows() -> None:
    planner = FakePlanner([AskUserAction("请指定供应商编码", "缺少关键参数")])
    runner, store, _, executor, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "NEEDS_INPUT"
    assert store.run.lease_owner is None
    assert store.run.finished_at is None
    plan = store.steps[0]
    assert plan.answer == "请指定供应商编码"
    assert plan.plan_summary == "缺少关键参数"
    assert [step.kind for step in store.steps] == ["PLAN"]
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_ask_user_reason_containing_ambiguous_pauses_ambiguous() -> None:
    planner = FakePlanner(
        [AskUserAction("你指的是哪个供应商？", "The request is AMBIGUOUS between two entities")]
    )
    runner, store, _, _, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "AMBIGUOUS"
    assert store.run.pause_detail is None


# ---------------------------------------------------------------------------
# 3. sequential tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_tool_actions_produce_plan_tool_plan_tool_plan_in_order() -> None:
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert [step.kind for step in store.steps] == [
        "PLAN",
        "TOOL",
        "PLAN",
        "TOOL",
        "PLAN",
    ]
    assert [step.step_no for step in store.steps] == [0, 1, 2, 3, 4]
    assert [step.iteration_no for step in store.steps] == [1, 1, 2, 2, 3]
    assert [step.action for step in store.steps if step.kind == "PLAN"] == [
        "CALL_TOOL",
        "CALL_TOOL",
        "FINISH",
    ]
    assert executor.invocations == [{"query": "a"}, {"query": "b"}]
    assert store.run.tool_call_count == 2
    assert store.run.iteration_count == 3
    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "done"


# ---------------------------------------------------------------------------
# 4. planner corrections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_errors_are_retried_bounded_then_pause_planner_invalid() -> None:
    planner = FakePlanner(
        [PlannerProtocolError("bad action") for _ in range(MAX_CORRECTIONS + 1)]
    )
    runner, store, _, executor, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert len(planner.calls) == 1 + MAX_CORRECTIONS
    assert planner.calls[1]["correction"] == "bad action"
    assert planner.calls[2]["correction"] == "bad action"
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "PLANNER_OUTPUT_INVALID"
    assert [step.kind for step in store.steps] == ["PLAN"]
    assert store.steps[0].status == "RUNNING"
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_tool_argument_protocol_error_is_corrected_on_the_same_plan() -> None:
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"wrong": 1}, "bad args"),
            CallToolAction("wiki_search", {"query": "ok"}, "good args"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, _ = buildRunner(
        planner=planner,
        prepareErrors=[PlannerProtocolError("tool 'wiki_search' arguments rejected: (wrong)")],
    )

    await driveRunner(runner)

    assert len(planner.calls) == 3
    assert planner.calls[1]["correction"] is not None
    assert "arguments rejected" in planner.calls[1]["correction"]
    plans = [step for step in store.steps if step.kind == "PLAN"]
    assert len(plans) == 2
    # The first PLAN was never completed with the rejected action.
    assert plans[0].status == "SUCCEEDED"
    assert plans[0].input_json == {"query": "ok"}
    tools = [step for step in store.steps if step.kind == "TOOL"]
    assert len(tools) == 1
    assert tools[0].input_json == {"query": "ok"}


# ---------------------------------------------------------------------------
# 5. permission denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_denial_pauses_immediately_without_handler_or_feedback() -> None:
    planner = FakePlanner([CallToolAction("wiki_search", {"query": "q"}, "lookup")])
    runner, store, planner, executor, _ = buildRunner(
        planner=planner,
        prepareErrors=[
            OrchestrationPauseError("PERMISSION_DENIED", "当前 Agent 无权调用该工具")
        ],
    )

    await driveRunner(runner)

    assert len(planner.calls) == 1
    assert planner.calls[0]["correction"] is None
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "PERMISSION_DENIED"
    assert executor.invocations == []
    assert [step.kind for step in store.steps] == ["PLAN"]
    # 暂停不携带任何策略细节：detail 为空，且文案只有通用拒绝语。
    assert store.run.pause_detail is None


# ---------------------------------------------------------------------------
# 6. budgets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "budget"),
    [
        ({"iteration_count": 10}, "iteration"),
        ({"tool_call_count": 10}, "tool"),
        ({"cost": 10.0}, "cost"),
        ({"started_at": NOW - timedelta(seconds=601)}, "time"),
    ],
)
async def test_budget_exhaustion_prevents_the_next_planner_call(
    overrides: dict[str, Any], budget: str
) -> None:
    run = makeRun(**overrides)
    runner, store, planner, executor, _ = buildRunner(run=run)

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "BUDGET_EXHAUSTED"
    assert store.run.pause_detail == {"budget": budget}
    assert store.steps == []


@pytest.mark.asyncio
async def test_legal_finish_crossing_cost_still_succeeds_after_metering() -> None:
    def _finishOverBudget(run, steps):
        run.cost = 11.0  # the planner call itself crossed the cost ceiling
        return FinishAction("over budget but legal")

    planner = FakePlanner([_finishOverBudget])
    runner, store, _, _, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "over budget but legal"
    assert store.run.pause_code is None


@pytest.mark.asyncio
async def test_cost_crossed_by_the_planner_blocks_the_next_handler_call() -> None:
    def _callOverBudget(run, steps):
        run.cost = 11.0
        return CallToolAction("wiki_search", {"query": "q"}, "lookup")

    planner = FakePlanner([_callOverBudget])
    runner, store, _, executor, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "BUDGET_EXHAUSTED"
    assert store.run.pause_detail == {"budget": "cost"}
    # The valid action is recorded so a resume does not need another Planner call.
    assert [step.kind for step in store.steps] == ["PLAN"]
    assert store.steps[0].action == "CALL_TOOL"
    assert store.steps[0].status == "SUCCEEDED"


