"""Task 4 unit tests: ACL delegate + two-phase orchestration tool executor.

The executor is exercised with an in-memory state fake (no SQLite, no PG): a
fake session dispatches ORM SELECTs by table name, a fake registry answers
``get`` / ``reload_one`` from memory and a real ``AgentRuntimeService`` performs
the pure policy check. Every rejection scenario asserts the handler was never
awaited — ``prepare`` must not execute tools.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.agent_orchestration_models import (
    AgentOrchestrationBinding,
    AgentOrchestrationProfile,
)
from app.domain.enums import AgentPermission, AgentStatus
from app.domain.exceptions import PermissionDeniedError
from app.domain.models import AgentAccessPolicy, AgentDefinition, AgentToolConfig
from app.services.agent_orchestration_tool_executor import (
    AgentOrchestrationToolExecutor,
)
from app.services.agent_orchestration_types import (
    OrchestrationPauseError,
    PlannerProtocolError,
    PreparedTool,
)
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.agent_tool_types import AgentTool, AgentToolContext, ToolResult

_AGENT_CODE = "ORCH_TEST_AGENT"
_TOOL_NAME = "wiki_search"
_DATA_OBJECT = "WIKI_PAGE"

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}
_ARGS = {"query": "开机流程"}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _agent(
    *,
    status: str = AgentStatus.ACTIVE.value,
    policies: list[AgentAccessPolicy] | None = None,
) -> AgentDefinition:
    entity = AgentDefinition(
        agent_code=_AGENT_CODE,
        agent_name="orchestration test agent",
        description="unit",
        trigger_type="user_question",
        response_latency="realtime",
        data_domains=["PROCUREMENT"],
        data_layers=["DIM"],
        status=status,
        owner="procurement",
        version="v1.0",
    )
    entity.policies = list(policies if policies is not None else [_policy()])
    return entity


def _policy(
    *,
    data_object: str = _DATA_OBJECT,
    permission: str = AgentPermission.READ.value,
    data_layer: str | None = None,
) -> AgentAccessPolicy:
    return AgentAccessPolicy(
        data_object=data_object,
        permission=permission,
        data_layer=data_layer,
        notes=None,
    )


def _profile(*, enabled: bool = True) -> AgentOrchestrationProfile:
    return AgentOrchestrationProfile(
        agent_id=1,
        instructions="answer with tools",
        enabled=enabled,
        revision=1,
    )


def _binding(*, enabled: bool = True) -> AgentOrchestrationBinding:
    return AgentOrchestrationBinding(
        profile_id=1,
        tool_name=_TOOL_NAME,
        enabled=enabled,
        sort_order=0,
    )


def _toolConfig(**overrides) -> AgentToolConfig:
    values = {
        "name": _TOOL_NAME,
        "description": "wiki search",
        "data_object": _DATA_OBJECT,
        "data_layers": ["DIM"],
        "input_schema": dict(_SCHEMA),
        "handler_kind": "BUILTIN",
        "handler_ref": "wiki_search",
        "arg_extractor_kind": "wiki_text",
        "enabled": True,
        "version": 1,
    }
    values.update(overrides)
    return AgentToolConfig(**values)


def _tool(
    *,
    handler=None,
    data_layers: tuple[str, ...] = ("DIM",),
    data_object: str = _DATA_OBJECT,
) -> AgentTool:
    async def defaultHandler(session, args, ctx):  # noqa: ARG001
        return ToolResult(data={"ok": True}, answer="ok")

    return AgentTool(
        name=_TOOL_NAME,
        description="wiki search",
        data_object=data_object,
        data_layers=data_layers,
        input_schema=dict(_SCHEMA),
        arg_extractor=lambda raw: {},  # noqa: ARG005
        handler=handler or defaultHandler,
    )


def _runRow(*, snapshot: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=1,
        agent_code=_AGENT_CODE,
        tool_bindings_snapshot=(
            snapshot
            if snapshot is not None
            else [{"toolName": _TOOL_NAME, "enabled": True, "sortOrder": 0}]
        ),
    )


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


_MISSING = object()


class _State:
    """In-memory current-state repository backing the fake session.

    Explicit ``None`` means "row is gone"; omitting an argument yields the
    healthy default row.
    """

    def __init__(
        self,
        *,
        agent=_MISSING,
        profile=_MISSING,
        binding=_MISSING,
        toolConfig=_MISSING,
    ) -> None:
        self.agent = _agent() if agent is _MISSING else agent
        self.profile = _profile() if profile is _MISSING else profile
        self.binding = _binding() if binding is _MISSING else binding
        self.toolConfig = _toolConfig() if toolConfig is _MISSING else toolConfig


class _FakeSession:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.queries: list[str] = []

    async def execute(self, stmt):
        text = str(stmt)
        self.queries.append(text)
        if "FROM agent_orchestration_binding" in text:
            return _FakeResult(
                [self.state.binding] if self.state.binding is not None else []
            )
        if "FROM agent_orchestration_profile" in text:
            return _FakeResult(
                [self.state.profile] if self.state.profile is not None else []
            )
        if "FROM agent_tool_config" in text:
            return _FakeResult(
                [self.state.toolConfig] if self.state.toolConfig is not None else []
            )
        if "FROM agent_definition" in text:
            return _FakeResult(
                [self.state.agent] if self.state.agent is not None else []
            )
        raise AssertionError(f"unexpected query: {text}")


class _FakeGuard:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def assertExecutionAllowed(self, session, run, leaseOwner, now) -> None:
        self.calls.append((session, run, leaseOwner, now))
        if self.error is not None:
            raise self.error


class _FakeRegistry:
    """In-memory tool registry double (no DB access).

    ``reloadTools`` simulates re-assembly from the current DB row: entries
    replace whatever stale tool was cached under the same name.
    """

    def __init__(
        self,
        tools: dict[str, AgentTool],
        reloadTools: dict[str, AgentTool] | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._reloadTools = dict(reloadTools or {})
        self.reloadCalls: list[str] = []

    def get(self, name: str):
        return self._tools.get(name)

    async def reload_one(self, session, name) -> None:  # noqa: ARG002
        self.reloadCalls.append(name)
        if name in self._reloadTools:
            self._tools[name] = self._reloadTools[name]


def _build(
    *,
    state: _State | None = None,
    tools: dict[str, AgentTool] | None = None,
    guard: _FakeGuard | None = None,
    reloadTools: dict[str, AgentTool] | None = None,
) -> tuple[
    AgentOrchestrationToolExecutor,
    _State,
    _FakeSession,
    _FakeGuard,
    _FakeRegistry,
    AsyncMock,
]:
    state = state or _State()
    guard = guard or _FakeGuard()
    handler = AsyncMock(return_value=ToolResult(data={"ok": True}, answer="ok"))
    registry = _FakeRegistry(
        tools if tools is not None else {_TOOL_NAME: _tool(handler=handler)},
        reloadTools=reloadTools,
    )
    executor = AgentOrchestrationToolExecutor(
        executionGuard=guard,
        toolRegistry=registry,
        runtime=AgentRuntimeService(registry=registry),
    )
    return executor, state, _FakeSession(state), guard, registry, handler


def _prepare(executor, session, run=None):
    return executor.prepare(
        session=session,
        run=run or _runRow(),
        toolName=_TOOL_NAME,
        args=dict(_ARGS),
        leaseOwner="worker-1",
        now=datetime(2026, 10, 27, tzinfo=UTC),
    )


class TestPrepareSuccess:
    @pytest.mark.asyncio
    async def test_prepare_returns_frozen_prepared_tool_without_calling_handler(self):
        handler = AsyncMock(return_value=ToolResult(data={"ok": True}, answer="ok"))
        executor, _, session, guard, _, _ = _build(
            tools={_TOOL_NAME: _tool(handler=handler)}
        )

        prepared = await _prepare(executor, session)

        assert isinstance(prepared, PreparedTool)
        assert prepared.tool.name == _TOOL_NAME
        assert prepared.args == _ARGS
        assert prepared.args is not _ARGS
        assert handler.await_count == 0
        assert len(guard.calls) == 1
        assert guard.calls[0][2] == "worker-1"

    @pytest.mark.asyncio
    async def test_invoke_is_the_only_handler_call_site(self):
        result = ToolResult(data={"hit": 1}, answer="found")
        handler = AsyncMock(return_value=result)
        executor, _, session, _, _, _ = _build(tools={_TOOL_NAME: _tool(handler=handler)})
        prepared = await _prepare(executor, session)
        context = AgentToolContext(actor="orchestration")

        invoked = await executor.invoke(session, prepared, context)

        assert invoked is result
        handler.assert_awaited_once()
        callArgs = handler.await_args.args
        assert callArgs[0] is session
        assert callArgs[1] == _ARGS
        assert callArgs[1] is not prepared.args
        assert callArgs[2] is context

    @pytest.mark.asyncio
    async def test_guard_runs_before_any_state_load(self):
        guard = _FakeGuard()
        guard.error = OrchestrationPauseError("BUDGET_EXHAUSTED", "budget exhausted")
        executor, _, session, _, _, handler = _build(guard=guard)

        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)

        assert excInfo.value.code == "BUDGET_EXHAUSTED"
        assert session.queries == []
        assert handler.await_count == 0


class TestCurrentToolResolution:
    """The executed tool must come from the current row, not a stale cache."""

    @pytest.mark.asyncio
    async def test_reloads_and_executes_current_config_over_stale_cached_tool(self):
        staleResult = ToolResult(data={"stale": True}, answer="stale")
        currentResult = ToolResult(data={"current": True}, answer="current")
        staleHandler = AsyncMock(return_value=staleResult)
        currentHandler = AsyncMock(return_value=currentResult)
        # Simulates a cache entry assembled from an older handler_ref:
        # different data object/layers that this Agent is not authorized for.
        stale = _tool(
            handler=staleHandler,
            data_layers=("FEATURE",),
            data_object="SUPPLIER",
        )
        current = _tool(handler=currentHandler)
        executor, _, session, _, registry, _ = _build(
            tools={_TOOL_NAME: stale},
            reloadTools={_TOOL_NAME: current},
        )

        prepared = await _prepare(executor, session)
        context = AgentToolContext(actor="orchestration")
        invoked = await executor.invoke(session, prepared, context)

        assert registry.reloadCalls == [_TOOL_NAME]
        assert prepared.tool is current
        assert invoked is currentResult
        assert currentHandler.await_count == 1
        assert staleHandler.await_count == 0


class TestPlannerMistakes:
    """Unknown tool / bad arguments are Planner corrections, never pauses."""

    @pytest.mark.asyncio
    async def test_tool_absent_from_run_snapshot_is_planner_error(self):
        executor, _, session, _, _, handler = _build()
        with pytest.raises(PlannerProtocolError):
            await _prepare(executor, session, run=_runRow(snapshot=[]))
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_schema_mismatch_is_planner_error(self):
        executor, _, session, _, _, handler = _build()
        with pytest.raises(PlannerProtocolError):
            await executor.prepare(
                session=session,
                run=_runRow(),
                toolName=_TOOL_NAME,
                args={"query": 123},
                leaseOwner="worker-1",
                now=datetime(2026, 10, 27, tzinfo=UTC),
            )
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_unknown_argument_key_is_planner_error(self):
        executor, _, session, _, _, handler = _build()
        with pytest.raises(PlannerProtocolError, match="arguments"):
            await executor.prepare(
                session=session,
                run=_runRow(),
                toolName=_TOOL_NAME,
                args={"query": "x", "unexpected": True},
                leaseOwner="worker-1",
                now=datetime(2026, 10, 27, tzinfo=UTC),
            )
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_invalid_current_schema_is_planner_error(self):
        state = _State(toolConfig=_toolConfig(input_schema={}))
        executor, _, session, _, _, handler = _build(state=state)
        with pytest.raises(PlannerProtocolError):
            await _prepare(executor, session)
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_planner_error_does_not_leak_argument_values(self):
        executor, _, session, _, _, handler = _build()
        secret = "supplier-秘密-100001"
        with pytest.raises(PlannerProtocolError) as excInfo:
            await executor.prepare(
                session=session,
                run=_runRow(),
                toolName=_TOOL_NAME,
                args={"query": "x", "leaked": secret},
                leaseOwner="worker-1",
                now=datetime(2026, 10, 27, tzinfo=UTC),
            )
        assert secret not in str(excInfo.value)
        assert handler.await_count == 0


class TestCurrentStateLoss:
    """Deleted/disabled agent, profile, binding, tool or handler → AGENT_UNAVAILABLE."""

    @pytest.mark.asyncio
    async def test_missing_agent_pauses(self):
        executor, _, session, _, _, handler = _build(state=_State(agent=None))
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_non_active_agent_pauses(self):
        executor, _, session, _, _, handler = _build(
            state=_State(agent=_agent(status=AgentStatus.DRAFT.value))
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_profile_pauses(self):
        executor, _, session, _, _, handler = _build(state=_State(profile=None))
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_disabled_profile_pauses(self):
        executor, _, session, _, _, handler = _build(
            state=_State(profile=_profile(enabled=False))
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_current_binding_pauses(self):
        executor, _, session, _, _, handler = _build(state=_State(binding=None))
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_disabled_binding_pauses(self):
        executor, _, session, _, _, handler = _build(
            state=_State(binding=_binding(enabled=False))
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_tool_config_pauses(self):
        executor, _, session, _, _, handler = _build(state=_State(toolConfig=None))
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_disabled_tool_config_pauses(self):
        executor, _, session, _, _, handler = _build(
            state=_State(toolConfig=_toolConfig(enabled=False))
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_non_builtin_handler_pauses(self):
        executor, _, session, _, _, handler = _build(
            state=_State(toolConfig=_toolConfig(handler_kind="NL2SQL"))
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_handler_ref_removed_from_whitelist_pauses(self):
        executor, _, session, _, _, handler = _build(
            state=_State(toolConfig=_toolConfig(handler_ref="nl2sql_default"))
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_tool_missing_from_registry_pauses(self):
        executor, _, session, _, _, _ = _build(tools={})
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "AGENT_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_handler_is_never_called_on_state_loss(self):
        handler = AsyncMock(return_value=ToolResult(data={}, answer="never"))
        state = _State(toolConfig=_toolConfig(enabled=False))
        executor, _, session, _, _, _ = _build(
            state=state, tools={_TOOL_NAME: _tool(handler=handler)}
        )
        with pytest.raises(OrchestrationPauseError):
            await _prepare(executor, session)
        assert handler.await_count == 0


class TestAclDenial:
    """ACL refusals are pauses with a generic message (no policy internals)."""

    @pytest.mark.asyncio
    async def test_deny_by_default_pauses_with_permission_denied(self):
        handler = AsyncMock(return_value=ToolResult(data={}, answer="never"))
        executor, _, session, _, _, _ = _build(
            state=_State(agent=_agent(policies=[])),
            tools={_TOOL_NAME: _tool(handler=handler)},
        )
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "PERMISSION_DENIED"
        assert not isinstance(excInfo.value, PlannerProtocolError)
        assert "FORBIDDEN" not in excInfo.value.message
        assert _DATA_OBJECT not in excInfo.value.message
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_forbidden_layer_overrides_wildcard_read(self):
        agent = _agent(
            policies=[
                _policy(data_layer=None),
                _policy(
                    data_layer="DIM", permission=AgentPermission.FORBIDDEN.value
                ),
            ]
        )
        executor, _, session, _, _, handler = _build(state=_State(agent=agent))
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "PERMISSION_DENIED"
        assert "FORBIDDEN" not in excInfo.value.message
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_historical_lowercase_forbidden_layer_is_enforced(self):
        agent = _agent(
            policies=[
                _policy(data_layer=None),
                _policy(
                    data_layer="dim", permission=AgentPermission.FORBIDDEN.value
                ),
            ]
        )
        executor, _, session, _, _, handler = _build(state=_State(agent=agent))
        with pytest.raises(OrchestrationPauseError) as excInfo:
            await _prepare(executor, session)
        assert excInfo.value.code == "PERMISSION_DENIED"
        assert handler.await_count == 0

    @pytest.mark.asyncio
    async def test_wildcard_layer_read_allows_layered_tool(self):
        agent = _agent(policies=[_policy(data_layer=None)])
        executor, _, session, _, _, _ = _build(
            state=_State(agent=agent),
            tools={_TOOL_NAME: _tool(data_layers=("DIM", "FEATURE"))},
        )
        prepared = await _prepare(executor, session)
        assert prepared.tool.name == _TOOL_NAME

    @pytest.mark.asyncio
    async def test_lowercase_historical_grant_is_honoured(self):
        agent = _agent(policies=[_policy(data_layer=" dim ")])
        executor, _, session, _, _, handler = _build(state=_State(agent=agent))
        prepared = await _prepare(executor, session)
        assert prepared.tool.name == _TOOL_NAME
        assert handler.await_count == 0


# ---------------------------------------------------------------------------
# Public ACL delegate — parity with the private policy engine
# ---------------------------------------------------------------------------


def _outcome(service: AgentRuntimeService, entity, tool):
    try:
        service.enforcePolicies(entity, tool)
        return None
    except PermissionDeniedError as exc:
        return (type(exc).__name__, exc.message)


def _privateOutcome(service: AgentRuntimeService, entity, tool):
    try:
        service._enforcePolicies(entity, tool)
        return None
    except PermissionDeniedError as exc:
        return (type(exc).__name__, exc.message)


class TestEnforcePoliciesDelegate:
    def test_delegate_matches_private_engine_for_every_policy_shape(self):
        service = AgentRuntimeService()
        cases = [
            # deny-by-default
            (_agent(policies=[]), _tool()),
            # FORBIDDEN overrides wildcard READ
            (
                _agent(
                    policies=[
                        _policy(data_layer=None),
                        _policy(
                            data_layer="DIM",
                            permission=AgentPermission.FORBIDDEN.value,
                        ),
                    ]
                ),
                _tool(data_layers=("DIM",)),
            ),
            # wildcard layers (None READ covers FEATURE and DIM)
            (
                _agent(policies=[_policy(data_layer=None)]),
                _tool(data_layers=("FEATURE", "DIM")),
            ),
            # lowercase historical layer names
            (
                _agent(policies=[_policy(data_layer=" dim ")]),
                _tool(data_layers=("DIM",)),
            ),
            # masked read grants access
            (
                _agent(
                    policies=[
                        _policy(permission=AgentPermission.MASKED_READ.value)
                    ]
                ),
                _tool(data_layers=()),
            ),
        ]
        for entity, tool in cases:
            assert _outcome(service, entity, tool) == _privateOutcome(
                service, entity, tool
            )

    def test_delegate_denies_by_default(self):
        service = AgentRuntimeService()
        with pytest.raises(PermissionDeniedError):
            service.enforcePolicies(_agent(policies=[]), _tool())

    def test_delegate_allows_wildcard_layer(self):
        service = AgentRuntimeService()
        service.enforcePolicies(
            _agent(policies=[_policy(data_layer=None)]),
            _tool(data_layers=("FEATURE", "DIM")),
        )
