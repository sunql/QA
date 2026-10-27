"""Task 9 unit tests: bounded runner state machine, budgets and pause semantics.

The runner is driven by an in-memory store/planner/executor/metering fake: no
database and no provider are involved. The fake store implements the same
conditional-transition contract as ``AgentOrchestrationStore`` (RUNNING lease,
plan/tool allocation, completion, terminal transitions) so the assertions pin
the *runner's* decisions: which step is created when, which call is withheld by
a budget, and how every terminal transition is persisted.

Crash replay itself is covered against real PostgreSQL in
``test_agent_orchestration_runner_recovery.py``; this module covers the
in-process recovery dispatch (a persisted RUNNING/SUCCEEDED step must be
resumed without a second Planner call where the contract says so).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.domain.exceptions import LlmClientError
from app.services.agent_orchestration_runner import AgentOrchestrationRunner
from app.services.agent_orchestration_types import (
    AskUserAction,
    CallToolAction,
    FinishAction,
    LeaseLostError,
    MeteredCompletion,
    OrchestrationPauseError,
    PlannerProtocolError,
    PreparedTool,
)
from app.services.agent_tool_types import AgentTool, AgentToolContext, ToolResult

_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_WORKER = "worker-test"
_LEASE_SECONDS = 60

_MAX_CORRECTIONS = 2
_MAX_TOOL_FAILURES = 2
_INFRA_RETRY_LIMIT = 2


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _SessionContext:
    async def __aenter__(self) -> Any:
        return SimpleNamespace()

    async def __aexit__(self, *excInfo: Any) -> bool:
        return False


class _FakeSessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


class _FakeStep:
    def __init__(self, **overrides: Any) -> None:
        values: dict[str, Any] = {
            "id": 1,
            "run_id": 1,
            "step_no": 0,
            "iteration_no": 1,
            "kind": "PLAN",
            "status": "RUNNING",
            "action": None,
            "plan_summary": None,
            "tool_name": None,
            "input_json": None,
            "output_json": None,
            "answer": None,
            "error_code": None,
            "error_message": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost": 0,
            "started_at": _NOW,
            "finished_at": None,
        }
        values.update(overrides)
        self.__dict__.update(values)


def _run(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": 1,
        "status": "RUNNING",
        "lease_owner": _WORKER,
        "lease_until": _NOW + timedelta(seconds=_LEASE_SECONDS),
        "cancel_requested": False,
        "iteration_count": 0,
        "tool_call_count": 0,
        "max_iterations": 10,
        "max_tool_calls": 10,
        "max_cost_usd": 10.0,
        "cost": 0.0,
        "timeout_seconds": 600,
        "started_at": _NOW - timedelta(seconds=10),
        "max_observation_chars": 2000,
        "created_by": "tester",
        "model_config_id": 7,
        "final_answer": None,
        "pause_code": None,
        "pause_detail": None,
        "error_code": None,
        "error_message": None,
        "finished_at": None,
        "revision": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeStore:
    """In-memory implementation of the Task 9 transition contract."""

    def __init__(self, run: SimpleNamespace | None = None) -> None:
        self.run = run or _run()
        self.steps: list[_FakeStep] = []
        self._nextId = 1
        self.calls: list[str] = []
        self.guardError: Exception | None = None
        self.renewResult = True
        self.completePlanError: Exception | None = None
        self.completeToolError: Exception | None = None
        self.getToolError: Exception | None = None
        self.succeedError: Exception | None = None
        self.pauseError: Exception | None = None

    # -- guard / lease ---------------------------------------------------

    async def assertExecutionAllowed(self, session, run, leaseOwner, now) -> None:
        self.calls.append("guard")
        if self.guardError is not None:
            raise self.guardError
        if run.cancel_requested:
            raise LeaseLostError("cancelled")
        if run.status != "RUNNING":
            raise LeaseLostError("not_running")
        if run.lease_owner != leaseOwner:
            raise LeaseLostError("lease_owner_mismatch")
        if run.lease_until is None or run.lease_until <= now:
            raise LeaseLostError("lease_expired")
        self.assertBudgets(run, now)

    def assertBudgets(self, run: SimpleNamespace, now: datetime) -> None:
        if run.iteration_count >= run.max_iterations:
            _raiseBudget("iteration")
        if run.tool_call_count >= run.max_tool_calls:
            _raiseBudget("tool")
        if run.cost >= run.max_cost_usd:
            _raiseBudget("cost")
        if (
            run.started_at is not None
            and now - run.started_at >= timedelta(seconds=run.timeout_seconds)
        ):
            _raiseBudget("time")

    async def renewLease(self, session, runId, workerId, leaseSeconds, now) -> bool:
        self.calls.append("renewLease")
        return self.renewResult

    # -- steps -----------------------------------------------------------

    def _appendStep(self, **overrides: Any) -> _FakeStep:
        maxNo = max((step.step_no for step in self.steps), default=-1)
        step = _FakeStep(
            id=self._nextId,
            run_id=self.run.id,
            step_no=maxNo + 1,
            **overrides,
        )
        self._nextId += 1
        self.steps.append(step)
        return step

    async def getOrCreatePlanStep(self, session, run, leaseOwner, now) -> _FakeStep:
        self.calls.append("getOrCreatePlanStep")
        existing = next(
            (
                step
                for step in self.steps
                if step.kind == "PLAN" and step.status == "RUNNING"
            ),
            None,
        )
        if existing is not None:
            return existing
        step = self._appendStep(
            kind="PLAN", iteration_no=run.iteration_count + 1
        )
        run.iteration_count += 1
        return step

    async def getOrCreateToolStep(
        self, session, run, planStep, leaseOwner, now, toolName, args
    ) -> _FakeStep:
        self.calls.append("getOrCreateToolStep")
        if self.getToolError is not None:
            raise self.getToolError
        existing = next(
            (
                step
                for step in self.steps
                if step.kind == "TOOL"
                and step.iteration_no == planStep.iteration_no
            ),
            None,
        )
        if existing is not None:
            return existing
        step = self._appendStep(
            kind="TOOL",
            iteration_no=planStep.iteration_no,
            tool_name=toolName,
            input_json=dict(args),
        )
        run.tool_call_count += 1
        return step

    async def completePlanStep(
        self, session, runId, stepId, leaseOwner, now, action
    ) -> None:
        self.calls.append("completePlanStep")
        if self.completePlanError is not None:
            raise self.completePlanError
        # 真实 store 在新会话里提交，调用方持有的对象保持提交前的样子（陈旧）。
        # 这里同样换成新对象，防止 Runner 靠内存别名读到尚未重新查询的字段。
        persisted = _FakeStep(**vars(self._step(stepId)))
        _applyPlanAction(persisted, action)
        persisted.status = "SUCCEEDED"
        persisted.finished_at = now
        index = next(
            i for i, step in enumerate(self.steps) if step.id == stepId
        )
        self.steps[index] = persisted

    async def completeToolStep(
        self,
        session,
        runId,
        stepId,
        leaseOwner,
        now,
        *,
        status,
        output=None,
        errorCode=None,
        errorMessage=None,
    ) -> None:
        self.calls.append("completeToolStep")
        if self.completeToolError is not None:
            raise self.completeToolError
        step = self._step(stepId)
        step.status = status
        step.output_json = output
        step.error_code = errorCode
        step.error_message = errorMessage
        step.finished_at = now

    def _step(self, stepId: int) -> _FakeStep:
        for step in self.steps:
            if step.id == stepId:
                return step
        raise AssertionError(f"unknown step {stepId}")

    # -- run transitions -------------------------------------------------

    async def pauseRun(
        self,
        session,
        runId,
        leaseOwner,
        code,
        detail,
        now,
        *,
        consumedStepId=None,
    ) -> None:
        self.calls.append("pauseRun")
        if self.pauseError is not None:
            raise self.pauseError
        self.run.status = "PAUSED"
        self.run.pause_code = code
        self.run.pause_detail = detail
        self.run.lease_owner = None
        self.run.lease_until = None
        if consumedStepId is not None:
            self._step(consumedStepId).status = "SKIPPED"

    async def succeedRun(self, session, runId, leaseOwner, answer, now) -> None:
        self.calls.append("succeedRun")
        if self.succeedError is not None:
            raise self.succeedError
        self.run.status = "SUCCEEDED"
        self.run.final_answer = answer
        self.run.lease_owner = None
        self.run.lease_until = None
        self.run.finished_at = now

    async def cancelRun(self, session, runId, leaseOwner, now) -> None:
        self.calls.append("cancelRun")
        self.run.status = "CANCELLED"
        self.run.lease_owner = None
        self.run.lease_until = None
        self.run.finished_at = now

    async def failRun(
        self, session, runId, leaseOwner, errorCode, errorMessage, now
    ) -> None:
        self.calls.append("failRun")
        self.run.status = "FAILED"
        self.run.error_code = errorCode
        self.run.error_message = errorMessage
        self.run.lease_owner = None
        self.run.lease_until = None
        self.run.finished_at = now


def _raiseBudget(budget: str) -> None:
    raise OrchestrationPauseError(
        "BUDGET_EXHAUSTED", "编排运行预算已耗尽", {"budget": budget}
    )


def _applyPlanAction(step: _FakeStep, action: Any) -> None:
    if isinstance(action, CallToolAction):
        step.action = "CALL_TOOL"
        step.tool_name = action.toolName
        step.input_json = dict(action.args)
        step.plan_summary = action.summary
    elif isinstance(action, AskUserAction):
        step.action = "ASK_USER"
        step.answer = action.question
        step.plan_summary = action.reason
    elif isinstance(action, FinishAction):
        step.action = "FINISH"
        step.answer = action.answer
    else:  # pragma: no cover - defensive test helper
        raise AssertionError(f"unknown action {action!r}")


class _FakeReader:
    def __init__(self, store: _FakeStore, *, hasModelConfig: bool = True) -> None:
        self.store = store
        self.hasModelConfig = hasModelConfig
        self.config = SimpleNamespace(id=7, model_name="fake-model", is_active=True)

    async def loadRun(self, runId: int) -> Any:
        return self.store.run

    async def loadSteps(self, runId: int) -> list[_FakeStep]:
        return list(self.store.steps)

    async def loadModelConfig(self, run: Any) -> Any:
        return self.config if self.hasModelConfig else None


def _tool() -> AgentTool:
    async def handler(session, args, context):  # noqa: ARG001
        return ToolResult(data={}, answer="")

    return AgentTool(
        name="wiki_search",
        description="search",
        data_object="WIKI_PAGE",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
        arg_extractor=lambda raw: {},  # noqa: ARG005
        handler=handler,
    )


class _FakePlanner:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def plan(self, run, steps, client, correction=None):
        self.calls.append(
            {"run": run, "steps": list(steps), "correction": correction}
        )
        if not self.responses:
            return FinishAction("default answer")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(run, steps)
        return item


class _FakeExecutor:
    """Prepare mirrors the real executor's guard-first contract."""

    def __init__(
        self,
        store: _FakeStore,
        *,
        handler: Any = None,
        prepareErrors: list[Exception] | None = None,
        invokeErrors: list[Exception] | None = None,
    ) -> None:
        self.store = store
        self.handler = handler or (lambda args, context: ToolResult(data={"ok": True}, answer="ok"))
        self.prepareErrors = list(prepareErrors or [])
        self.invokeErrors = list(invokeErrors or [])
        self.prepares: list[dict[str, Any]] = []
        self.invocations: list[dict[str, Any]] = []
        self.contexts: list[AgentToolContext] = []

    async def prepare(self, session, run, toolName, args, leaseOwner, now):
        await self.store.assertExecutionAllowed(session, run, leaseOwner, now)
        if self.prepareErrors:
            raise self.prepareErrors.pop(0)
        self.prepares.append({"toolName": toolName, "args": dict(args)})
        return PreparedTool(tool=_tool(), args=dict(args))

    async def invoke(self, session, prepared, context):
        self.contexts.append(context)
        self.invocations.append(dict(prepared.args))
        # 模拟工具内部调用 LLM：只为让计量工厂签发一个客户端。
        if context.llm_factory is not None:
            context.llm_factory(None)
        if self.invokeErrors:
            raise self.invokeErrors.pop(0)
        return self.handler(dict(prepared.args), context)


class _FakeMeteredClient:
    def __init__(self, factory: _FakeMeteringFactory) -> None:
        self.factory = factory

    async def completeForPlanning(self, messages):
        return MeteredCompletion(
            response=SimpleNamespace(content="{}"), usageId=1
        )

    async def flushPendingRecords(self) -> None:
        self.factory.drains.append(len(self.factory.clients))

    async def close(self) -> None:  # pragma: no cover - not used by the runner
        return None


class _FakeMeteringFactory:
    """Callable factory double; records the step it was built for."""

    instances: list[_FakeMeteringFactory] = []

    def __init__(self, sessionFactory, runId, stepId, callerType, config) -> None:
        self.runId = runId
        self.stepId = stepId
        self.callerType = callerType
        self.config = config
        self.clients: list[_FakeMeteredClient] = []
        self.drains: list[int] = []
        _FakeMeteringFactory.instances.append(self)

    def __call__(self, ignored: Any = None) -> _FakeMeteredClient:
        client = _FakeMeteredClient(self)
        self.clients.append(client)
        return client


def _build(
    *,
    run: SimpleNamespace | None = None,
    planner: _FakePlanner | None = None,
    handler: Any = None,
    prepareErrors: list[Exception] | None = None,
    invokeErrors: list[Exception] | None = None,
    hasModelConfig: bool = True,
) -> tuple[
    AgentOrchestrationRunner,
    _FakeStore,
    _FakePlanner,
    _FakeExecutor,
    _FakeMeteringFactory,
]:
    store = _FakeStore(run)
    planner = planner or _FakePlanner()
    executor = _FakeExecutor(
        store,
        handler=handler,
        prepareErrors=prepareErrors,
        invokeErrors=invokeErrors,
    )
    reader = _FakeReader(store, hasModelConfig=hasModelConfig)
    _FakeMeteringFactory.instances = []
    runner = AgentOrchestrationRunner(
        sessionFactory=_FakeSessionFactory(),
        store=store,  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        reader=reader,  # type: ignore[arg-type]
        settings=SimpleNamespace(
            agentOrchestrationLeaseSeconds=_LEASE_SECONDS,
            agentOrchestrationMaxPlannerCorrections=_MAX_CORRECTIONS,
            agentOrchestrationMaxToolFailures=_MAX_TOOL_FAILURES,
        ),
        meteredFactory=_FakeMeteringFactory,  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    return runner, store, planner, executor, _FakeMeteringFactory


async def _drive(
    runner: AgentOrchestrationRunner, runId: int = 1
) -> None:
    await asyncio.wait_for(runner.runClaimed(runId, _WORKER), timeout=5)


# ---------------------------------------------------------------------------
# 1. FINISH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_writes_the_answer_on_plan_and_run() -> None:
    planner = _FakePlanner([FinishAction("供应商 1001 由采购部负责")])
    runner, store, planner, executor, _ = _build(planner=planner)

    await _drive(runner)

    plan = store.steps[0]
    assert plan.kind == "PLAN"
    assert plan.action == "FINISH"
    assert plan.answer == "供应商 1001 由采购部负责"
    assert plan.status == "SUCCEEDED"
    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "供应商 1001 由采购部负责"
    assert store.run.finished_at == _NOW
    assert executor.invocations == []
    assert len(planner.calls) == 1


# ---------------------------------------------------------------------------
# 2. ASK_USER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_pauses_needs_input_without_a_tool_rows() -> None:
    planner = _FakePlanner([AskUserAction("请指定供应商编码", "缺少关键参数")])
    runner, store, _, executor, _ = _build(planner=planner)

    await _drive(runner)

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
    planner = _FakePlanner(
        [AskUserAction("你指的是哪个供应商？", "The request is AMBIGUOUS between two entities")]
    )
    runner, store, _, _, _ = _build(planner=planner)

    await _drive(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "AMBIGUOUS"
    assert store.run.pause_detail is None


# ---------------------------------------------------------------------------
# 3. sequential tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_tool_actions_produce_plan_tool_plan_tool_plan_in_order() -> None:
    planner = _FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, _ = _build(planner=planner)

    await _drive(runner)

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
    planner = _FakePlanner(
        [PlannerProtocolError("bad action") for _ in range(_MAX_CORRECTIONS + 1)]
    )
    runner, store, _, executor, _ = _build(planner=planner)

    await _drive(runner)

    assert len(planner.calls) == 1 + _MAX_CORRECTIONS
    assert planner.calls[1]["correction"] == "bad action"
    assert planner.calls[2]["correction"] == "bad action"
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "PLANNER_OUTPUT_INVALID"
    assert [step.kind for step in store.steps] == ["PLAN"]
    assert store.steps[0].status == "RUNNING"
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_tool_argument_protocol_error_is_corrected_on_the_same_plan() -> None:
    planner = _FakePlanner(
        [
            CallToolAction("wiki_search", {"wrong": 1}, "bad args"),
            CallToolAction("wiki_search", {"query": "ok"}, "good args"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, _ = _build(
        planner=planner,
        prepareErrors=[PlannerProtocolError("tool 'wiki_search' arguments rejected: (wrong)")],
    )

    await _drive(runner)

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
    planner = _FakePlanner([CallToolAction("wiki_search", {"query": "q"}, "lookup")])
    runner, store, planner, executor, _ = _build(
        planner=planner,
        prepareErrors=[
            OrchestrationPauseError("PERMISSION_DENIED", "当前 Agent 无权调用该工具")
        ],
    )

    await _drive(runner)

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
        ({"started_at": _NOW - timedelta(seconds=601)}, "time"),
    ],
)
async def test_budget_exhaustion_prevents_the_next_planner_call(
    overrides: dict[str, Any], budget: str
) -> None:
    run = _run(**overrides)
    runner, store, planner, executor, _ = _build(run=run)

    await _drive(runner)

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

    planner = _FakePlanner([_finishOverBudget])
    runner, store, _, _, _ = _build(planner=planner)

    await _drive(runner)

    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "over budget but legal"
    assert store.run.pause_code is None


@pytest.mark.asyncio
async def test_cost_crossed_by_the_planner_blocks_the_next_handler_call() -> None:
    def _callOverBudget(run, steps):
        run.cost = 11.0
        return CallToolAction("wiki_search", {"query": "q"}, "lookup")

    planner = _FakePlanner([_callOverBudget])
    runner, store, _, executor, _ = _build(planner=planner)

    await _drive(runner)

    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "BUDGET_EXHAUSTED"
    assert store.run.pause_detail == {"budget": "cost"}
    # The valid action is recorded so a resume does not need another Planner call.
    assert [step.kind for step in store.steps] == ["PLAN"]
    assert store.steps[0].action == "CALL_TOOL"
    assert store.steps[0].status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# 7. tool failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_consecutive_tool_exceptions_pause_tool_failed() -> None:
    planner = _FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            FinishAction("never"),
        ]
    )
    runner, store, planner, executor, _ = _build(
        planner=planner,
        invokeErrors=[RuntimeError("secret db dsn"), RuntimeError("secret again")],
    )

    await _drive(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "TOOL_FAILED"
    assert len(planner.calls) == 2
    assert len(executor.invocations) == 2
    tools = [step for step in store.steps if step.kind == "TOOL"]
    assert [tool.status for tool in tools] == ["FAILED", "FAILED"]
    failed = tools[0]
    assert "secret" not in (failed.error_message or "")
    assert "secret" not in (failed.output_json or {}).get("text", "")
    assert failed.error_code == "TOOL_EXECUTION_FAILED"
    # The failed observation is what the Planner sees as feedback.
    history = planner.calls[1]["steps"]
    failedStep = next(step for step in history if step.kind == "TOOL")
    assert "工具执行失败" in failedStep.output_json["text"]


@pytest.mark.asyncio
async def test_resume_with_two_committed_tool_failures_pauses_without_replanning() -> None:
    """崩溃在「TOOL FAILED 提交」与「暂停写入」之间：已提交行重建判定。"""
    runner, store, planner, executor, _ = _build(planner=_FakePlanner())
    for query in ("a", "b"):
        store._appendStep(
            kind="PLAN",
            status="SUCCEEDED",
            action="CALL_TOOL",
            tool_name="wiki_search",
            input_json={"query": query},
        )
        store._appendStep(
            kind="TOOL",
            status="FAILED",
            tool_name="wiki_search",
            input_json={"query": query},
            error_code="TOOL_EXECUTION_FAILED",
        )
    store.run.iteration_count = 2
    store.run.tool_call_count = 2

    await _drive(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "TOOL_FAILED"


@pytest.mark.asyncio
async def test_a_successful_tool_resets_the_consecutive_failure_counter() -> None:
    planner = _FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "a"}, "first"),
            CallToolAction("wiki_search", {"query": "b"}, "second"),
            CallToolAction("wiki_search", {"query": "c"}, "third"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, _ = _build(planner=planner)

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

    await _drive(runner)

    assert store.run.status == "SUCCEEDED"
    tools = [step for step in store.steps if step.kind == "TOOL"]
    assert [tool.status for tool in tools] == ["FAILED", "SUCCEEDED", "FAILED"]


# ---------------------------------------------------------------------------
# 8. infrastructure vs permanent invariant failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_infrastructure_errors_are_retried_then_pause() -> None:
    planner = _FakePlanner(
        [LlmClientError("provider down sk-secret") for _ in range(10)]
    )
    runner, store, _, executor, _ = _build(planner=planner)

    await _drive(runner)

    assert len(planner.calls) == 1 + _INFRA_RETRY_LIMIT
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "INFRASTRUCTURE_ERROR"
    assert store.run.pause_detail is None
    assert store.run.error_message is None
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_prepare_infrastructure_errors_are_retried_then_pause() -> None:
    planner = _FakePlanner(
        [CallToolAction("wiki_search", {"query": "q"}, "lookup")]
    )
    runner, store, _, executor, _ = _build(
        planner=planner,
        prepareErrors=[SQLAlchemyError("db down") for _ in range(10)],
    )

    await _drive(runner)

    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "INFRASTRUCTURE_ERROR"
    assert len(executor.prepares) == 0
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_permanent_invariant_violation_fails_the_run_with_a_sanitized_error() -> None:
    planner = _FakePlanner([FinishAction("done")])
    runner, store, _, _, _ = _build(planner=planner)
    store.completePlanError = RuntimeError("secret internal detail")

    await _drive(runner)

    assert store.run.status == "FAILED"
    assert store.run.error_code == "INTERNAL_ERROR"
    assert "secret" not in (store.run.error_message or "")
    assert store.run.final_answer is None


@pytest.mark.asyncio
async def test_escaping_infrastructure_error_leaves_the_run_for_lease_retry() -> None:
    runner, store, planner, executor, _ = _build(planner=_FakePlanner())
    store.guardError = LlmClientError("provider down")

    await _drive(runner)

    # 外部依赖故障不是「继续执行不安全」：不写终态，等租约过期后由新 worker 接手。
    assert store.run.status == "RUNNING"
    assert store.run.pause_code is None
    assert store.run.error_code is None
    assert planner.calls == []
    assert executor.invocations == []


@pytest.mark.asyncio
async def test_missing_model_config_pauses_before_any_llm_call() -> None:
    runner, store, planner, executor, _ = _build(
        planner=_FakePlanner([FinishAction("done")]),
        hasModelConfig=False,
    )

    await _drive(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "INFRASTRUCTURE_ERROR"


# ---------------------------------------------------------------------------
# 9/10. cancellation and stale leases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_cancel_is_observed_after_the_current_tool_finishes() -> None:
    planner = _FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "q"}, "lookup"),
            FinishAction("never reached"),
        ]
    )

    def handler(args, context):
        store.run.cancel_requested = True
        return ToolResult(data={"ok": True}, answer="ok")

    runner, store, planner, executor, _ = _build(planner=planner, handler=handler)

    await _drive(runner)

    assert store.run.status == "CANCELLED"
    assert store.run.finished_at == _NOW
    assert len(executor.invocations) == 1
    assert len(planner.calls) == 1
    tool = next(step for step in store.steps if step.kind == "TOOL")
    assert tool.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_cancel_already_requested_on_a_reclaimed_run_is_finalized_without_work() -> None:
    run = _run(cancel_requested=True)
    runner, store, planner, executor, _ = _build(run=run)

    await _drive(runner)

    assert store.run.status == "CANCELLED"
    assert planner.calls == []
    assert executor.invocations == []
    assert "cancelRun" in store.calls


@pytest.mark.asyncio
async def test_stale_lease_stops_without_any_call_or_write() -> None:
    run = _run()
    runner, store, planner, executor, _ = _build(run=run)
    store.guardError = LeaseLostError("lease_expired")

    await _drive(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "RUNNING"
    assert "cancelRun" not in store.calls
    assert "pauseRun" not in store.calls


@pytest.mark.asyncio
async def test_failed_lease_renewal_stops_before_planning() -> None:
    runner, store, planner, executor, _ = _build()
    store.renewResult = False

    await _drive(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "RUNNING"


# ---------------------------------------------------------------------------
# persisted-state recovery dispatch
# ---------------------------------------------------------------------------


def _seedPlan(store: _FakeStore, **overrides: Any) -> _FakeStep:
    values: dict[str, Any] = {
        "kind": "PLAN",
        "status": "SUCCEEDED",
        "iteration_no": 1,
    }
    values.update(overrides)
    step = store._appendStep(**values)
    store.run.iteration_count = max(store.run.iteration_count, step.iteration_no)
    return step


@pytest.mark.asyncio
async def test_committed_call_tool_plan_is_reconstructed_without_replanning() -> None:
    runner, store, planner, executor, _ = _build(
        planner=_FakePlanner([FinishAction("done")])
    )
    plan = _seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "persisted"},
        plan_summary="lookup",
    )

    await _drive(runner)

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
    runner, store, planner, executor, _ = _build(
        planner=_FakePlanner(),
        prepareErrors=[
            PlannerProtocolError("tool 'wiki_search' arguments rejected: (query)")
        ],
    )
    plan = _seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "persisted"},
        plan_summary="lookup",
    )

    await _drive(runner)

    # 旧参数在新的 schema 下永远不可能通过：不得重新调用 Planner 空转。
    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "PLANNER_OUTPUT_INVALID"
    assert plan.status == "SKIPPED"


@pytest.mark.asyncio
async def test_committed_finish_plan_succeeds_without_replanning() -> None:
    runner, store, planner, executor, _ = _build(planner=_FakePlanner())
    _seedPlan(store, action="FINISH", answer="already planned")

    await _drive(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "SUCCEEDED"
    assert store.run.final_answer == "already planned"


@pytest.mark.asyncio
async def test_committed_ask_user_plan_pauses_and_is_marked_consumed() -> None:
    runner, store, planner, executor, _ = _build(planner=_FakePlanner())
    plan = _seedPlan(
        store, action="ASK_USER", answer="which supplier?", plan_summary="ambiguous wording"
    )

    await _drive(runner)

    assert planner.calls == []
    assert executor.invocations == []
    assert store.run.status == "PAUSED"
    assert store.run.pause_code == "AMBIGUOUS"
    assert plan.status == "SKIPPED"


@pytest.mark.asyncio
async def test_running_plan_is_reused_by_replanning_without_a_second_iteration() -> None:
    runner, store, planner, executor, _ = _build(
        planner=_FakePlanner([FinishAction("done")])
    )
    plan = _seedPlan(store, status="RUNNING")
    store.run.iteration_count = 1

    await _drive(runner)

    assert len([step for step in store.steps if step.kind == "PLAN"]) == 1
    assert store.steps[0].id == plan.id
    assert store.run.iteration_count == 1
    assert len(planner.calls) == 1


@pytest.mark.asyncio
async def test_running_tool_is_replayed_with_the_original_arguments() -> None:
    runner, store, planner, executor, _ = _build(
        planner=_FakePlanner([FinishAction("done")])
    )
    plan = _seedPlan(
        store,
        action="CALL_TOOL",
        tool_name="wiki_search",
        input_json={"query": "original"},
        plan_summary="lookup",
    )
    tool = store._appendStep(
        kind="TOOL",
        iteration_no=plan.iteration_no,
        tool_name="wiki_search",
        input_json={"query": "original"},
        status="RUNNING",
    )
    store.run.tool_call_count = 1

    await _drive(runner)

    assert executor.invocations == [{"query": "original"}]
    assert len([step for step in store.steps if step.kind == "TOOL"]) == 1
    assert store.steps[1].id == tool.id
    assert tool.status == "SUCCEEDED"
    assert store.run.tool_call_count == 1
    assert len(planner.calls) == 1
    assert store.run.status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# metering wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_step_builds_its_own_metered_factory_and_drains_it() -> None:
    planner = _FakePlanner(
        [
            CallToolAction("wiki_search", {"query": "q"}, "lookup"),
            FinishAction("done"),
        ]
    )
    runner, store, _, executor, factoryClass = _build(planner=planner)

    await _drive(runner)

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
