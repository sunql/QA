"""Task 9 崩溃恢复集成测试的**共享 support**（双测替身 + fixtures + 落库助手）。

背景：`test_agent_orchestration_runner_recovery.py` 与
`test_agent_orchestration_runner_recovery_pause.py` 两个套件共用同一批
崩溃矩阵夹具。抽到本模块后：

- 依赖关系显式：谁提供什么，一眼可见；
- 两个套件各自只保留自己关心的用例，行数都在 800 行上限内；
- 与 `app/tests/_orchestration_support.py` 的既有做法一致。

本模块只提供**测试替身**与**落库助手**，不复制被测业务逻辑：Runner、Store、
计量全部是真实实现，只有 provider 边界（`createClient`）与工具执行器被替身。
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
    AgentOrchestrationBinding,
    AgentOrchestrationProfile,
    AgentOrchestrationRun,
    AgentOrchestrationStep,
    AgentOrchestrationUsage,
)
from app.domain.exceptions import ValidationError
from app.domain.models import AgentDefinition, AgentToolConfig, LlmConfig
from app.infrastructure.llm.base_client import (
    BaseLlmClient,
    LlmMessage,
    LlmResponse,
    StreamChunk,
)
from app.services import agent_orchestration_metering
from app.services.agent_orchestration_runner import AgentOrchestrationRunner
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_orchestration_types import PlannerProtocolError, PreparedTool
from app.services.agent_orchestration_validation import validateToolArguments
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


class RecordingExecutor:
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
        return PreparedTool(tool=fakeTool(), args=dict(args))

    async def invoke(self, session, prepared, context):
        self.invocations.append(dict(prepared.args))
        self.contexts.append(context)
        return self._handler(dict(prepared.args))


class SchemaCheckedExecutor(RecordingExecutor):
    """按**当前** AgentToolConfig 的 input_schema 校验参数的执行器替身。

    真实执行器（``AgentOrchestrationToolExecutor._validateArgs``）的责任分两
    段：先校验存储的 schema 本身合法，再校验 Planner 参数。本替身复刻第二段
    的判定（同样的 ``validateToolArguments``），因此「管理员收紧了 schema」
    这一配置变更能被 Runner 真实地观察到 —— 参数不匹配时抛
    ``PlannerProtocolError``，与生产路径完全一致。
    """

    async def prepare(self, session, run, toolName, args, leaseOwner, now):
        config = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == toolName)
            )
        ).scalar_one_or_none()
        assert config is not None, f"tool {toolName!r} must be configured"
        try:
            validateToolArguments(config.input_schema or {}, dict(args))
        except ValidationError as exc:
            raise PlannerProtocolError(
                f"tool {toolName!r} arguments rejected: {exc.message}"
            ) from exc
        return await super().prepare(session, run, toolName, args, leaseOwner, now)


def raiseToolError(args: dict[str, Any]) -> ToolResult:
    raise RuntimeError(f"tool handler blew up for {args!r}")


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


class ScriptedLlmClient(BaseLlmClient):
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


class CrashingStore(AgentOrchestrationStore):
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
) -> Callable[[list[str]], ScriptedLlmClient]:
    """Patch the provider factory; each scenario gets its own script."""

    def _install(responses: list[str]) -> ScriptedLlmClient:
        client = ScriptedLlmClient(responses)
        monkeypatch.setattr(
            agent_orchestration_metering, "createClient", lambda config: client
        )
        return client

    return _install


async def seedRun(
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
                    "inputSchema": wikiSearchSchema(),
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


async def seedResumePrerequisites(
    factory: async_sessionmaker[AsyncSession],
    *,
    agentCode: str = "runner-agent",
) -> None:
    """造出 Task 7 ``resumeRun`` 会重新校验的少量治理行。

    Runner 自己只依赖 run 快照；但真实恢复服务会重新加载 Agent、profile、
    绑定与模型，并重建一次工具快照。这里直接落库这些行，测试的恢复动作
    仍然走**真实服务**，不复制它的业务逻辑。
    """
    async with factory() as session:
        agent = AgentDefinition(
            agent_code=agentCode,
            agent_name="runner agent",
            description="orchestration runner test agent",
            data_domains=["PROCUREMENT"],
            data_layers=["DIM"],
            status="active",
        )
        session.add(agent)
        await session.flush()
        profile = AgentOrchestrationProfile(
            agent_id=agent.id,
            instructions="answer supplier questions read-only",
            enabled=True,
        )
        session.add(profile)
        await session.flush()
        session.add(
            AgentOrchestrationBinding(
                profile_id=profile.id,
                tool_name="wiki_search",
                enabled=True,
                sort_order=0,
            )
        )
        session.add(wikiSearchToolConfig())
        await session.commit()


def wikiSearchToolConfig() -> AgentToolConfig:
    """编排可用的只读工具行（wiki_search）。"""
    return AgentToolConfig(
        name="wiki_search",
        description="search wiki",
        data_object="WIKI_PAGE",
        data_layers=[],
        input_schema=wikiSearchSchema(),
        handler_kind="BUILTIN",
        handler_ref="wiki_search",
        enabled=True,
    )


def wikiSearchSchema() -> dict[str, Any]:
    """wiki_search 的初始 schema：只有一个可选的 query 字符串。"""
    return {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    }


async def tightenToolSchema(
    factory: async_sessionmaker[AsyncSession], toolName: str = "wiki_search"
) -> None:
    """管理员收紧工具 schema：新增必填 limit，落库的旧参数不再匹配。"""
    async with factory() as session:
        config = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == toolName)
            )
        ).scalar_one()
        config.input_schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        }
        await session.commit()


async def claimRun(
    factory: async_sessionmaker[AsyncSession], runId: int, workerId: str
) -> None:
    now = datetime.now(UTC)
    async with factory() as session:
        claimed = await AgentOrchestrationStore().claimNext(
            session, workerId, LEASE_SECONDS, now
        )
    assert claimed is not None and claimed.id == runId


async def expireLeaseAndReclaim(
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
    await claimRun(factory, runId, WORKER_2)


def buildRunner(
    factory: async_sessionmaker[AsyncSession],
    *,
    store: AgentOrchestrationStore | None = None,
    executor: RecordingExecutor | None = None,
) -> AgentOrchestrationRunner:
    return AgentOrchestrationRunner(
        sessionFactory=factory,
        store=store or AgentOrchestrationStore(),
        executor=executor or RecordingExecutor(),  # type: ignore[arg-type]
        settings=SimpleNamespace(
            agentOrchestrationLeaseSeconds=LEASE_SECONDS,
            agentOrchestrationMaxPlannerCorrections=2,
            agentOrchestrationMaxToolFailures=2,
        ),
    )


async def loadRun(
    factory: async_sessionmaker[AsyncSession], runId: int
) -> AgentOrchestrationRun:
    async with factory() as session:
        run = await session.get(AgentOrchestrationRun, runId)
        assert run is not None
        return run


async def loadSteps(
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


async def loadUsage(
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

