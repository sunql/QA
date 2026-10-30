"""Task 9 单元测试的**共享 support**（Runner 的内存替身与驱动助手）。

背景：Runner 的单元测试拆成三个套件（核心状态机 / 失败与租约 / 已提交步骤
恢复），共用同一批内存替身。抽到本模块后：

- 依赖关系显式：谁提供什么，一眼可见；
- 三个套件各自只保留自己关心的用例，行数都在 800 行上限内；
- 与 `app/tests/_orchestration_support.py`、`app/tests/_orchestration_recovery_support.py`
  的既有做法一致（跨模块只引用**公开**名字，避免私有名被改名后变成
  收集期 error）。

替身实现的是与 ``AgentOrchestrationStore`` 相同的条件转移契约，因此断言钉住
的是 **Runner 的决策**：什么时候建哪一步、预算挡下哪一次调用、每一次终态
转移如何落库。真实数据库上的崩溃恢复由
``app/tests/integration/test_agent_orchestration_runner_recovery.py`` 覆盖。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from app.services.agent_orchestration_runner import AgentOrchestrationRunner
from app.services.agent_orchestration_types import (
    AskUserAction,
    CallToolAction,
    FinishAction,
    LeaseLostError,
    MeteredCompletion,
    OrchestrationPauseError,
    PreparedTool,
)
from app.services.agent_tool_types import AgentTool, AgentToolContext, ToolResult

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
WORKER = "worker-test"
LEASE_SECONDS = 60

MAX_CORRECTIONS = 2
MAX_TOOL_FAILURES = 2
INFRA_RETRY_LIMIT = 2


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class SessionContext:
    async def __aenter__(self) -> Any:
        return SimpleNamespace()

    async def __aexit__(self, *excInfo: Any) -> bool:
        return False


class FakeSessionFactory:
    def __call__(self) -> SessionContext:
        return SessionContext()


class FakeStep:
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
            "started_at": NOW,
            "finished_at": None,
        }
        values.update(overrides)
        self.__dict__.update(values)


def makeRun(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": 1,
        "status": "RUNNING",
        "lease_owner": WORKER,
        "lease_until": NOW + timedelta(seconds=LEASE_SECONDS),
        "cancel_requested": False,
        "iteration_count": 0,
        "tool_call_count": 0,
        "max_iterations": 10,
        "max_tool_calls": 10,
        "max_cost_usd": 10.0,
        "cost": 0.0,
        "timeout_seconds": 600,
        "started_at": NOW - timedelta(seconds=10),
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


class FakeStore:
    """In-memory implementation of the Task 9 transition contract."""

    def __init__(self, run: SimpleNamespace | None = None) -> None:
        self.run = run or makeRun()
        self.steps: list[FakeStep] = []
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
            raiseBudget("iteration")
        if run.tool_call_count >= run.max_tool_calls:
            raiseBudget("tool")
        if run.cost >= run.max_cost_usd:
            raiseBudget("cost")
        if (
            run.started_at is not None
            and now - run.started_at >= timedelta(seconds=run.timeout_seconds)
        ):
            raiseBudget("time")

    async def renewLease(self, session, runId, workerId, leaseSeconds, now) -> bool:
        self.calls.append("renewLease")
        return self.renewResult

    # -- steps -----------------------------------------------------------

    def appendStep(self, **overrides: Any) -> FakeStep:
        maxNo = max((step.step_no for step in self.steps), default=-1)
        step = FakeStep(
            id=self._nextId,
            run_id=self.run.id,
            step_no=maxNo + 1,
            **overrides,
        )
        self._nextId += 1
        self.steps.append(step)
        return step

    async def getOrCreatePlanStep(self, session, run, leaseOwner, now) -> FakeStep:
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
        step = self.appendStep(
            kind="PLAN", iteration_no=run.iteration_count + 1
        )
        run.iteration_count += 1
        return step

    async def getOrCreateToolStep(
        self, session, run, planStep, leaseOwner, now, toolName, args
    ) -> FakeStep:
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
        step = self.appendStep(
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
        persisted = FakeStep(**vars(self._step(stepId)))
        applyPlanAction(persisted, action)
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

    def _step(self, stepId: int) -> FakeStep:
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
        consumedToolFailureIds=None,
        consumedRunningToolIds=None,
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
        for stepId in consumedToolFailureIds or []:
            step = self._step(stepId)
            assert step.status == "FAILED", "consumed TOOL row must still be FAILED"
            step.status = "SKIPPED"
        for stepId in consumedRunningToolIds or []:
            step = self._step(stepId)
            assert step.status == "RUNNING", "consumed TOOL row must still be RUNNING"
            step.status = "SKIPPED"

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


def raiseBudget(budget: str) -> None:
    raise OrchestrationPauseError(
        "BUDGET_EXHAUSTED", "编排运行预算已耗尽", {"budget": budget}
    )


def applyPlanAction(step: FakeStep, action: Any) -> None:
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


class FakeReader:
    def __init__(self, store: FakeStore, *, hasModelConfig: bool = True) -> None:
        self.store = store
        self.hasModelConfig = hasModelConfig
        self.config = SimpleNamespace(id=7, model_name="fake-model", is_active=True)

    async def loadRun(self, runId: int) -> Any:
        return self.store.run

    async def loadSteps(self, runId: int) -> list[FakeStep]:
        return list(self.store.steps)

    async def loadModelConfig(self, run: Any) -> Any:
        return self.config if self.hasModelConfig else None


def fakeTool() -> AgentTool:
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


class FakePlanner:
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


class FakeExecutor:
    """Prepare mirrors the real executor's guard-first contract."""

    def __init__(
        self,
        store: FakeStore,
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
        return PreparedTool(tool=fakeTool(), args=dict(args))

    async def invoke(self, session, prepared, context):
        self.contexts.append(context)
        self.invocations.append(dict(prepared.args))
        # 模拟工具内部调用 LLM：只为让计量工厂签发一个客户端。
        if context.llm_factory is not None:
            context.llm_factory(None)
        if self.invokeErrors:
            raise self.invokeErrors.pop(0)
        return self.handler(dict(prepared.args), context)


class FakeMeteredClient:
    def __init__(self, factory: FakeMeteringFactory) -> None:
        self.factory = factory

    async def completeForPlanning(self, messages):
        return MeteredCompletion(
            response=SimpleNamespace(content="{}"), usageId=1
        )

    async def flushPendingRecords(self) -> None:
        self.factory.drains.append(len(self.factory.clients))

    async def close(self) -> None:  # pragma: no cover - not used by the runner
        return None


class FakeMeteringFactory:
    """Callable factory double; records the step it was built for."""

    instances: list[FakeMeteringFactory] = []

    def __init__(self, sessionFactory, runId, stepId, callerType, config) -> None:
        self.runId = runId
        self.stepId = stepId
        self.callerType = callerType
        self.config = config
        self.clients: list[FakeMeteredClient] = []
        self.drains: list[int] = []
        FakeMeteringFactory.instances.append(self)

    def __call__(self, ignored: Any = None) -> FakeMeteredClient:
        client = FakeMeteredClient(self)
        self.clients.append(client)
        return client


def buildRunner(
    *,
    run: SimpleNamespace | None = None,
    planner: FakePlanner | None = None,
    handler: Any = None,
    prepareErrors: list[Exception] | None = None,
    invokeErrors: list[Exception] | None = None,
    hasModelConfig: bool = True,
) -> tuple[
    AgentOrchestrationRunner,
    FakeStore,
    FakePlanner,
    FakeExecutor,
    FakeMeteringFactory,
]:
    store = FakeStore(run)
    planner = planner or FakePlanner()
    executor = FakeExecutor(
        store,
        handler=handler,
        prepareErrors=prepareErrors,
        invokeErrors=invokeErrors,
    )
    reader = FakeReader(store, hasModelConfig=hasModelConfig)
    FakeMeteringFactory.instances = []
    runner = AgentOrchestrationRunner(
        sessionFactory=FakeSessionFactory(),
        store=store,  # type: ignore[arg-type]
        planner=planner,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        reader=reader,  # type: ignore[arg-type]
        settings=SimpleNamespace(
            agentOrchestrationLeaseSeconds=LEASE_SECONDS,
            agentOrchestrationMaxPlannerCorrections=MAX_CORRECTIONS,
            agentOrchestrationMaxToolFailures=MAX_TOOL_FAILURES,
        ),
        meteredFactory=FakeMeteringFactory,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return runner, store, planner, executor, FakeMeteringFactory


async def driveRunner(
    runner: AgentOrchestrationRunner, runId: int = 1
) -> None:
    await asyncio.wait_for(runner.runClaimed(runId, WORKER), timeout=5)


def resumeThroughTask7(store: FakeStore) -> None:
    """模拟 Task 7 ``resumeRun`` 的列级语义，然后重新 claim。

    真实服务只改 run 列（PAUSED → QUEUED、清 pause/lease/started_at、
    revision+1），**不碰 step 行**；worker 的 ``claimNext`` 同样只改 run 列。
    这里刻意照抄这两步，确保测试覆盖「步骤行完全由暂停/执行写入决定」。
    """
    store.run.status = "QUEUED"
    store.run.pause_code = None
    store.run.pause_detail = None
    store.run.error_code = None
    store.run.error_message = None
    store.run.lease_owner = None
    store.run.lease_until = None
    store.run.started_at = None
    store.run.revision += 1
    store.run.status = "RUNNING"
    store.run.lease_owner = WORKER
    store.run.lease_until = NOW + timedelta(seconds=LEASE_SECONDS)
    store.run.started_at = NOW


