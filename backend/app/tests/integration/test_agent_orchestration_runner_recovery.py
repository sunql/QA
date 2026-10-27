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
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationStep,
    AgentOrchestrationUsage,
)
from app.domain.models import LlmConfig
from app.infrastructure.llm.base_client import (
    BaseLlmClient,
    LlmMessage,
    LlmResponse,
    StreamChunk,
)
from app.services import agent_orchestration_metering
from app.services.agent_orchestration_runner import AgentOrchestrationRunner
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_orchestration_types import PreparedTool
from app.services.agent_tool_types import AgentTool, AgentToolContext, ToolResult
from app.tests import _pg_support

LEASE_SECONDS = 60
WORKER_1 = "worker-1"
WORKER_2 = "worker-2"

CALL_TOOL_JSON = (
    '{"action":"CALL_TOOL","toolName":"wiki_search",'
    '"args":{"query":"supplier 1001"},"summary":"lookup supplier"}'
)
FINISH_JSON = '{"action":"FINISH","answer":"supplier 1001 is owned by procurement"}'
ASK_USER_JSON = (
    '{"action":"ASK_USER","question":"which supplier code?",'
    '"reason":"the question does not name a supplier"}'
)
ASK_USER_AMBIGUOUS_JSON = (
    '{"action":"ASK_USER","question":"which supplier code?",'
    '"reason":"The request is AMBIGUOUS between two suppliers"}'
)


class SimulatedWorkerCrash(BaseException):
    """Process death between two durable writes (not an application error)."""


class _RecordingExecutor:
    def __init__(
        self,
        handler: Callable[[dict[str, Any]], ToolResult] | None = None,
    ) -> None:
        self.invocations: list[dict[str, Any]] = []
        self.contexts: list[AgentToolContext] = []
        self._handler = handler or (
            lambda args: ToolResult(data={"rows": 1}, answer="one supplier")
        )

    async def prepare(self, session, run, toolName, args, leaseOwner, now):
        return PreparedTool(tool=_fakeTool(), args=dict(args))

    async def invoke(self, session, prepared, context):
        self.invocations.append(dict(prepared.args))
        self.contexts.append(context)
        return self._handler(dict(prepared.args))


def _raiseToolError(args: dict[str, Any]) -> ToolResult:
    raise RuntimeError(f"tool handler blew up for {args!r}")


def _fakeTool() -> AgentTool:
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


class _ScriptedLlmClient(BaseLlmClient):
    """Deterministic provider boundary with a call-before assertion hook."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[LlmMessage]] = []
        self.onCall: Callable[[int], Awaitable[None]] | None = None

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        self.calls.append(list(messages))
        if self.onCall is not None:
            await self.onCall(len(self.calls))
        if not self._responses:
            raise AssertionError("unexpected extra LLM call")
        return LlmResponse(
            content=self._responses.pop(0),
            modelName="fake-model",
            promptTokens=10,
            completionTokens=5,
            totalTokens=15,
        )

    def completeStream(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError("streaming is not used by the orchestration runner")

    async def complete_with_tools(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("tool calling is not used by the orchestration runner")

    async def close(self) -> None:
        return None


class _CrashingStore(AgentOrchestrationStore):
    """Raises ``SimulatedWorkerCrash`` exactly once at a named transition."""

    def __init__(self, hook: str) -> None:
        self.hook = hook
        self.fired = False

    def _maybeCrash(self, hook: str) -> None:
        if self.hook == hook and not self.fired:
            self.fired = True
            raise SimulatedWorkerCrash(hook)

    async def getOrCreatePlanStep(self, *args: Any, **kwargs: Any):
        step = await super().getOrCreatePlanStep(*args, **kwargs)
        self._maybeCrash("after_get_plan")
        return step

    async def completePlanStep(self, *args: Any, **kwargs: Any) -> None:
        await super().completePlanStep(*args, **kwargs)
        self._maybeCrash("after_complete_plan")

    async def getOrCreateToolStep(self, *args: Any, **kwargs: Any):
        step = await super().getOrCreateToolStep(*args, **kwargs)
        self._maybeCrash("after_get_tool")
        return step

    async def completeToolStep(self, *args: Any, **kwargs: Any) -> None:
        self._maybeCrash("before_complete_tool")
        await super().completeToolStep(*args, **kwargs)
        self._maybeCrash("after_complete_tool")

    async def pauseRun(self, *args: Any, **kwargs: Any) -> None:
        # 崩溃点：暂停判定已做出、TOOL FAILED 已提交，但暂停尚未写入。
        self._maybeCrash("before_pause")
        await super().pauseRun(*args, **kwargs)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
async def sessionFactory(
    pg_engine: Any,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    await _pg_support._truncateAll(pg_engine)
    factory = async_sessionmaker(
        pg_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    yield factory
    await _pg_support._truncateAll(pg_engine)


@pytest.fixture()
def scriptedClient(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[str]], _ScriptedLlmClient]:
    """Patch the provider factory; each scenario gets its own script."""

    def _install(responses: list[str]) -> _ScriptedLlmClient:
        client = _ScriptedLlmClient(responses)
        monkeypatch.setattr(
            agent_orchestration_metering, "createClient", lambda config: client
        )
        return client

    return _install


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[AgentOrchestrationRun, LlmConfig]:
    async with factory() as session:
        config = LlmConfig(
            model_name="fake-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.01"),
            cost_per_1k_output=Decimal("0.02"),
            is_active=True,
        )
        session.add(config)
        await session.flush()
        run = AgentOrchestrationRun(
            run_id=str(uuid4()),
            agent_code="runner-agent",
            profile_revision=1,
            instructions_snapshot="answer supplier questions read-only",
            tool_bindings_snapshot=[
                {
                    "toolName": "wiki_search",
                    "description": "search wiki",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "sortOrder": 0,
                }
            ],
            max_observation_chars=2000,
            question="who owns supplier 1001?",
            created_by="tester",
            model_config_id=config.id,
            status="QUEUED",
            max_iterations=5,
            max_tool_calls=5,
            max_cost_usd=Decimal("1"),
            timeout_seconds=300,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        session.expunge(run)
        session.expunge(config)
        return run, config


async def _claim(
    factory: async_sessionmaker[AsyncSession], runId: int, workerId: str
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        claimed = await AgentOrchestrationStore().claimNext(
            session, workerId, LEASE_SECONDS, now
        )
    assert claimed is not None and claimed.id == runId


async def _expireLeaseAndReclaim(
    factory: async_sessionmaker[AsyncSession], runId: int
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        await session.execute(
            update(AgentOrchestrationRun)
            .where(AgentOrchestrationRun.id == runId)
            .values(lease_until=now - timedelta(seconds=5))
        )
        await session.commit()
    await _claim(factory, runId, WORKER_2)


def _runner(
    factory: async_sessionmaker[AsyncSession],
    *,
    store: AgentOrchestrationStore | None = None,
    executor: _RecordingExecutor | None = None,
) -> AgentOrchestrationRunner:
    return AgentOrchestrationRunner(
        sessionFactory=factory,
        store=store or AgentOrchestrationStore(),
        executor=executor or _RecordingExecutor(),  # type: ignore[arg-type]
        settings=SimpleNamespace(
            agentOrchestrationLeaseSeconds=LEASE_SECONDS,
            agentOrchestrationMaxPlannerCorrections=2,
            agentOrchestrationMaxToolFailures=2,
        ),
    )


async def _loadRun(
    factory: async_sessionmaker[AsyncSession], runId: int
) -> AgentOrchestrationRun:
    async with factory() as session:
        run = await session.get(AgentOrchestrationRun, runId)
        assert run is not None
        return run


async def _loadSteps(
    factory: async_sessionmaker[AsyncSession], runId: int
) -> list[AgentOrchestrationStep]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentOrchestrationStep)
                    .where(AgentOrchestrationStep.run_id == runId)
                    .order_by(AgentOrchestrationStep.step_no)
                )
            )
            .scalars()
            .all()
        )


async def _loadUsage(
    factory: async_sessionmaker[AsyncSession], runId: int
) -> list[AgentOrchestrationUsage]:
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentOrchestrationUsage)
                    .where(AgentOrchestrationUsage.run_id == runId)
                    .order_by(AgentOrchestrationUsage.id)
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# crash matrix
# ---------------------------------------------------------------------------


async def test_crash_after_tool_output_commits_continues_from_the_observation(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    client = scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    executor = _RecordingExecutor()
    crasher = _CrashingStore("after_complete_tool")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    crashedSteps = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in crashedSteps] == ["PLAN", "TOOL"]
    assert crashedSteps[1].status == "SUCCEEDED"
    assert crashedSteps[1].output_json is not None
    assert crashedSteps[1].output_json["text"]
    assert len(executor.invocations) == 1
    usageAfterCrash = await _loadUsage(sessionFactory, run.id)
    assert len(usageAfterCrash) == 1
    assert usageAfterCrash[0].step_id == crashedSteps[0].id
    planUsage = (usageAfterCrash[0].prompt_tokens, usageAfterCrash[0].completion_tokens)

    async def _assertCommittedBeforeReplan(callIndex: int) -> None:
        if callIndex != 2:
            return
        steps = await _loadSteps(sessionFactory, run.id)
        assert steps[1].kind == "TOOL"
        assert steps[1].status == "SUCCEEDED"
        assert steps[1].output_json is not None
        assert "one supplier" in steps[1].output_json["text"]

    client.onCall = _assertCommittedBeforeReplan

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    steps = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in steps] == ["PLAN", "TOOL", "PLAN"]
    assert len([step for step in steps if step.kind == "TOOL"]) == 1
    assert len(executor.invocations) == 1
    assert executor.invocations == [{"query": "supplier 1001"}]
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.final_answer == "supplier 1001 is owned by procurement"
    usage = await _loadUsage(sessionFactory, run.id)
    assert len(usage) == 2
    assert usage[0].id == usageAfterCrash[0].id
    assert (usage[0].prompt_tokens, usage[0].completion_tokens) == planUsage
    assert usage[1].step_id == steps[2].id


async def test_crash_after_tool_row_creation_replays_the_same_step_and_args(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    executor = _RecordingExecutor()
    crasher = _CrashingStore("after_get_tool")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    afterCrash = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN", "TOOL"]
    assert afterCrash[1].status == "RUNNING"
    toolStepId = afterCrash[1].id
    assert afterCrash[1].input_json == {"query": "supplier 1001"}
    assert executor.invocations == []

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    steps = await _loadSteps(sessionFactory, run.id)
    tools = [step for step in steps if step.kind == "TOOL"]
    assert len(tools) == 1
    assert tools[0].id == toolStepId
    assert tools[0].status == "SUCCEEDED"
    assert executor.invocations == [{"query": "supplier 1001"}]
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.tool_call_count == 1


async def test_crash_after_handler_before_output_commit_replays_the_handler(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    executor = _RecordingExecutor()
    crasher = _CrashingStore("before_complete_tool")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    stepsAfterCrash = await _loadSteps(sessionFactory, run.id)
    toolStep = next(step for step in stepsAfterCrash if step.kind == "TOOL")
    assert toolStep.status == "RUNNING"
    assert toolStep.output_json is None
    assert toolStep.step_no == 1
    firstArgs = list(executor.invocations)

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    assert len(executor.invocations) == 2
    assert executor.invocations == [firstArgs[0], firstArgs[0]]
    steps = await _loadSteps(sessionFactory, run.id)
    assert len([step for step in steps if step.kind == "TOOL"]) == 1
    assert steps[1].id == toolStep.id
    assert steps[1].status == "SUCCEEDED"
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.tool_call_count == 1


async def test_crash_during_running_plan_reuses_the_row_and_one_iteration(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    client = scriptedClient([FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    crasher = _CrashingStore("after_get_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher).runClaimed(run.id, WORKER_1)

    afterCrash = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN"]
    assert afterCrash[0].status == "RUNNING"
    planStepId = afterCrash[0].id
    crashedRun = await _loadRun(sessionFactory, run.id)
    assert crashedRun.iteration_count == 1
    assert client.calls == []

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory).runClaimed(run.id, WORKER_2)

    steps = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in steps] == ["PLAN"]
    assert steps[0].id == planStepId
    assert steps[0].status == "SUCCEEDED"
    assert steps[0].action == "FINISH"
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.iteration_count == 1
    assert persisted.status == "SUCCEEDED"
    assert len(client.calls) == 1


async def test_crash_after_call_tool_plan_commit_reconstructs_without_a_planner_call(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    client = scriptedClient([CALL_TOOL_JSON, FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    executor = _RecordingExecutor()
    crasher = _CrashingStore("after_complete_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher, executor=executor).runClaimed(
            run.id, WORKER_1
        )

    afterCrash = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN"]
    assert afterCrash[0].status == "SUCCEEDED"
    assert afterCrash[0].action == "CALL_TOOL"
    assert len(client.calls) == 1
    assert executor.invocations == []

    async def _assertNoReplanBeforeTool(callIndex: int) -> None:
        if callIndex != 2:
            return
        assert executor.invocations == [{"query": "supplier 1001"}]
        steps = await _loadSteps(sessionFactory, run.id)
        assert len([step for step in steps if step.kind == "TOOL"]) == 1

    client.onCall = _assertNoReplanBeforeTool

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    assert len(client.calls) == 2
    steps = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in steps] == ["PLAN", "TOOL", "PLAN"]
    assert steps[1].input_json == {"query": "supplier 1001"}
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"


async def test_crash_after_finish_plan_commit_succeeds_without_replanning(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    client = scriptedClient([FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    crasher = _CrashingStore("after_complete_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher).runClaimed(run.id, WORKER_1)

    afterCrash = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in afterCrash] == ["PLAN"]
    assert afterCrash[0].status == "SUCCEEDED"
    assert afterCrash[0].action == "FINISH"

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory).runClaimed(run.id, WORKER_2)

    assert len(client.calls) == 1
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "SUCCEEDED"
    assert persisted.final_answer == "supplier 1001 is owned by procurement"


async def test_crash_after_ask_user_plan_commit_pauses_without_replanning(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    client = scriptedClient([ASK_USER_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    crasher = _CrashingStore("after_complete_plan")

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(sessionFactory, store=crasher).runClaimed(run.id, WORKER_1)

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory).runClaimed(run.id, WORKER_2)

    assert len(client.calls) == 1
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "PAUSED"
    assert persisted.pause_code == "NEEDS_INPUT"
    steps = await _loadSteps(sessionFactory, run.id)
    assert [step.status for step in steps] == ["SKIPPED"]


async def test_reclaimed_run_with_cancellation_finalizes_without_any_llm_call(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    """A RUNNING run cancelled while its worker was dead must not resume work."""

    client = scriptedClient([FINISH_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)

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

    await _claim(sessionFactory, run.id, WORKER_2)
    await _runner(sessionFactory).runClaimed(run.id, WORKER_2)

    assert client.calls == []
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "CANCELLED"
    assert persisted.lease_owner is None
    assert persisted.finished_at is not None
    steps = await _loadSteps(sessionFactory, run.id)
    assert steps == []


# ---------------------------------------------------------------------------
# fix round 1: pause-code and crash-safety regressions
# ---------------------------------------------------------------------------


async def test_first_execution_ambiguous_reason_pauses_ambiguous(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    """The pause code comes from the ACTION's reason, not the stale step row."""

    client = scriptedClient([ASK_USER_AMBIGUOUS_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)

    await _runner(sessionFactory).runClaimed(run.id, WORKER_1)

    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "PAUSED"
    assert persisted.pause_code == "AMBIGUOUS"
    assert persisted.pause_detail is None
    assert len(client.calls) == 1
    steps = await _loadSteps(sessionFactory, run.id)
    assert [step.status for step in steps] == ["SKIPPED"]
    assert steps[0].plan_summary == "The request is AMBIGUOUS between two suppliers"


async def test_crash_between_tool_failure_commit_and_pause_does_not_replan(
    sessionFactory: async_sessionmaker[AsyncSession],
    scriptedClient: Callable[[list[str]], _ScriptedLlmClient],
) -> None:
    """Two committed FAILED rows must reproduce TOOL_FAILED without an LLM call."""

    client = scriptedClient([CALL_TOOL_JSON, CALL_TOOL_JSON])
    run, _ = await _seed(sessionFactory)
    await _claim(sessionFactory, run.id, WORKER_1)
    executor = _RecordingExecutor(handler=_raiseToolError)

    with pytest.raises(SimulatedWorkerCrash):
        await _runner(
            sessionFactory, store=_CrashingStore("before_pause"), executor=executor
        ).runClaimed(run.id, WORKER_1)

    crashed = await _loadRun(sessionFactory, run.id)
    assert crashed.status == "RUNNING"
    crashedSteps = await _loadSteps(sessionFactory, run.id)
    assert [step.kind for step in crashedSteps] == ["PLAN", "TOOL", "PLAN", "TOOL"]
    assert [step.status for step in crashedSteps] == [
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
        "FAILED",
    ]
    assert len(client.calls) == 2
    assert len(executor.invocations) == 2

    await _expireLeaseAndReclaim(sessionFactory, run.id)
    await _runner(sessionFactory, executor=executor).runClaimed(run.id, WORKER_2)

    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "PAUSED"
    assert persisted.pause_code == "TOOL_FAILED"
    # The committed FAILED rows already prove the threshold: no new planning.
    assert len(client.calls) == 2
    steps = await _loadSteps(sessionFactory, run.id)
    assert len([step for step in steps if step.kind == "TOOL"]) == 2
    assert len(executor.invocations) == 2
