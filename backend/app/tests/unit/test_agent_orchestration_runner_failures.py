"""Task 9 unit tests: tool failures, infrastructure errors and lease control.

Same in-memory doubles as ``test_agent_orchestration_runner.py`` (see
``app/tests/unit/_agent_orchestration_runner_support.py``), different subject:

- consecutive tool exceptions must pause with TOOL_FAILED at the threshold and
  must not be re-counted after a resume;
- infrastructure errors (provider / DB) must be retried and then parked for a
  lease retry instead of writing a terminal state;
- a genuine invariant violation must still fail the run with a sanitized error;
- cancellation and lease loss must stop the run without extra calls or writes.

共享的内存替身在 ``app/tests/unit/_agent_orchestration_runner_support.py``。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.domain.exceptions import LlmClientError
from app.services.agent_orchestration_types import (
    CallToolAction,
    FinishAction,
    LeaseLostError,
)
from app.services.agent_tool_types import ToolResult
from app.tests.unit._agent_orchestration_runner_support import (
    INFRA_RETRY_LIMIT,
    NOW,
    FakePlanner,
    buildRunner,
    driveRunner,
    makeRun,
    resumeThroughTask7,
)

# ---------------------------------------------------------------------------
# 7. tool failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_consecutive_tool_exceptions_pause_tool_failed() -> None:
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            FinishAction("never"),
        ]
    )
    runner, store, planner, executor, _ = buildRunner(
        planner=planner,
        invokeErrors=[RuntimeError("secret db dsn"), RuntimeError("secret again")],
    )

    await driveRunner(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "TOOL_FAILED"
    assert len(planner.calls) == 2
    assert len(executor.invocations) == 2
    tools = [step for step in store.steps if step.kind == "TOOL"]
    # 暂停写入时消费掉尾部失败行：这样用户恢复后能从干净的计数重新规划。
    assert [tool.status for tool in tools] == ["SKIPPED", "SKIPPED"]
    failed = tools[0]
    assert "secret" not in (failed.error_message or "")
    assert "secret" not in (failed.output_json or {}).get("text", "")
    assert failed.error_code == "TOOL_EXECUTION_FAILED"
    # The failed observation is what the Planner sees as feedback.
    history = planner.calls[1]["steps"]
    failedStep = next(step for step in history if step.kind == "TOOL")
    assert "工具执行失败" in failedStep.output_json["text"]


@pytest.mark.asyncio
async def test_crash_before_pause_with_two_committed_tool_failures_pauses_without_replanning() -> None:
    """崩溃在「TOOL FAILED 提交」与「暂停写入」之间：已提交行重建判定。"""
    runner, store, planner, executor, _ = buildRunner(planner=FakePlanner())
    for query in ("a", "b"):
        store.appendStep(
            kind="PLAN",
            status="SUCCEEDED",
            action="CALL_TOOL",
            tool_name="wiki_search",
            input_json={"query": query},
        )
        store.appendStep(
            kind="TOOL",
            status="FAILED",
            tool_name="wiki_search",
            input_json={"query": query},
            error_code="TOOL_EXECUTION_FAILED",
        )
    store.run.iteration_count = 2
    store.run.tool_call_count = 2

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "TOOL_FAILED"
    # 暂停写入时把崩溃窗口里的失败行一并消费，恢复后才不会再次触发阈值。
    tools = [step for step in store.steps if step.kind == "TOOL"]
    assert [tool.status for tool in tools] == ["SKIPPED", "SKIPPED"]


@pytest.mark.asyncio
async def test_resume_after_tool_failed_pause_replans_and_counts_fresh() -> None:
    """用户真正恢复后必须重新规划：失败预算由暂停一次性结算，不跨恢复累计。"""
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            CallToolAction("wiki_search", {"query": "c"}, "after resume"),
            FinishAction("done"),
        ]
    )
    runner, store, planner, executor, _ = buildRunner(
        planner=planner,
        invokeErrors=[
            RuntimeError("first"),
            RuntimeError("second"),
            RuntimeError("third"),
        ],
    )

    await driveRunner(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "TOOL_FAILED"
    assert len(planner.calls) == 2

    resumeThroughTask7(store)
    await driveRunner(runner)

    # 恢复后确实又调用了一次 Planner（不是 0 次），并且这次失败从 1 重新计数：
    # 若旧失败仍被计入，第三次失败会立刻再次暂停，而不是继续到 FINISH。
    assert len(planner.calls) == 4
    assert len(executor.invocations) == 3
    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "done"
    tools = [step for step in store.steps if step.kind == "TOOL"]
    assert [tool.status for tool in tools] == ["SKIPPED", "SKIPPED", "FAILED"]


@pytest.mark.asyncio
async def test_a_successful_tool_resets_the_consecutive_failure_counter() -> None:
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            CallToolAction("wiki_search", {"query": "c"}, "third"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, _ = buildRunner(planner=planner)

    # Sequence: fail(1), success(reset), fail(1), FINISH -> limit 2 never hit.
    # The second injected failure is consumed by the middle call only if the
    # first failure stays at 1; arrange outcomes explicitly below.
    results = iter(
        [
            "raise",
            "ok",
            "raise",
        ]
    )

    async def sequencedInvoke(session, prepared, context):
        executor.contexts.append(context)
        executor.invocations.append(dict(prepared.args))
        outcome = next(results)
        if outcome == "raise":
            raise RuntimeError("boom")
        return ToolResult(data={"ok": True}, answer="ok")

    executor.invoke = sequencedInvoke  # type: ignore[method-assign]

    await driveRunner(runner)

    assert store.run.status == "SUCCEEDED"
    tools = [step for step in store.steps if step.kind == "TOOL"]
    assert [tool.status for tool in tools] == ["FAILED", "SUCCEEDED", "FAILED"]


# ---------------------------------------------------------------------------
# 8. infrastructure vs permanent invariant failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_infrastructure_errors_are_retried_then_pause() -> None:
    planner = FakePlanner(
        [LlmClientError("provider down sk-secret") for _ in range(10)]
    )
    runner, store, _, executor, _ = buildRunner(planner=planner)

    await driveRunner(runner)

    assert len(planner.calls) == 1 + INFRA_RETRY_LIMIT
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "INFRASTRUCTURE_ERROR"
    assert store.run.pause_detail is None
    assert store.run.error_message is None
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_prepare_infrastructure_errors_are_retried_then_pause() -> None:
    planner = FakePlanner(
        [CallToolAction("wiki_search", {"query": "q"}, "lookup")]
    )
    runner, store, _, executor, _ = buildRunner(
        planner=planner,
        prepareErrors=[SQLAlchemyError("db down") for _ in range(10)],
    )

    await driveRunner(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "INFRASTRUCTURE_ERROR"
    assert len(executor.prepares) == 0
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_permanent_invariant_violation_fails_the_run_with_a_sanitized_error() -> None:
    planner = FakePlanner([FinishAction("done")])
    runner, store, _, _, _ = buildRunner(planner=planner)
    store.completePlanError = RuntimeError("secret internal detail")

    await driveRunner(runner)

    assert store.run.status == "FAILED"
    assert store.run.error_code == "INTERNAL_ERROR"
    assert "secret" not in (store.run.error_message or "")
    assert store.run.final_answer is None


@pytest.mark.asyncio
async def test_escaping_infrastructure_error_leaves_the_run_for_lease_retry() -> None:
    runner, store, planner, executor, _ = buildRunner(planner=FakePlanner())
    store.guardError = LlmClientError("provider down")

    await driveRunner(runner)

    # 外部依赖故障不是「继续执行不安全」：不写终态，等租约过期后由新 worker 接手。
    assert store.run.status == "RUNNING"
    assert store.run.pause_code is None
    assert store.run.error_code is None
    assert planner.calls == []
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_missing_model_config_pauses_before_any_llm_call() -> None:
    runner, store, planner, executor, _ = buildRunner(
        planner=FakePlanner([FinishAction("done")]),
        hasModelConfig=False,
    )

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "INFRASTRUCTURE_ERROR"


# ---------------------------------------------------------------------------
# 9/10. cancellation and stale leases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_cancel_is_observed_after_the_current_tool_finishes() -> None:
    planner = FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "q"}, "lookup"),
            FinishAction("never reached"),
        ]
    )

    def handler(args, context):
        store.run.cancel_requested = True
        return ToolResult(data={"ok": True}, answer="ok")

    runner, store, planner, executor, _ = buildRunner(planner=planner, handler=handler)

    await driveRunner(runner)

    assert store.run.status == "CANCELLED"
    assert store.run.finished_at == NOW
    assert len(executor.invocations) == 1
    assert len(planner.calls) == 1
    tool = next(step for step in store.steps if step.kind == "TOOL")
    assert tool.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_cancel_already_requested_on_a_reclaimed_run_is_finalized_without_work() -> None:
    run = makeRun(cancel_requested=True)
    runner, store, planner, executor, _ = buildRunner(run=run)

    await driveRunner(runner)

    assert store.run.status == "CANCELLED"
    assert planner.calls == []
    assert executor.invocations == []
    assert "cancelRun" in store.calls


@pytest.mark.asyncio
async def test_stale_lease_stops_without_any_call_or_write() -> None:
    run = makeRun()
    runner, store, planner, executor, _ = buildRunner(run=run)
    store.guardError = LeaseLostError("lease_expired")

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "RUNNING"
    assert "cancelRun" not in store.calls
    assert "pauseRun" not in store.calls


@pytest.mark.asyncio
async def test_failed_lease_renewal_stops_before_planning() -> None:
    runner, store, planner, executor, _ = buildRunner()
    store.renewResult = False

    await driveRunner(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "RUNNING"


