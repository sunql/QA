"""Two-phase tool executor for the general Agent orchestration subsystem.

Execution is split so that every safety decision happens **before** the run
creates a TOOL step:

- ``prepare`` re-reads current state (Agent / profile / binding / tool config),
  checks the run snapshot, validates Planner arguments against the current
  schema and applies the Agent ACL through ``AgentRuntimeService.enforcePolicies``.
  It never calls a tool handler.
- ``invoke`` is the single handler call site and only runs on a ``PreparedTool``.

Error semantics:
- Unknown tools and argument/schema mismatches are Planner mistakes and raise
  ``PlannerProtocolError`` (safe feedback: field paths only, never values).
- Deleted/disabled current state raises ``OrchestrationPauseError`` with
  ``AGENT_UNAVAILABLE``.
- ACL refusals raise ``OrchestrationPauseError`` with ``PERMISSION_DENIED`` and a
  generic message that never exposes policy internals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_orchestration_models import (
    AgentOrchestrationBinding,
    AgentOrchestrationProfile,
    AgentOrchestrationRun,
)
from app.domain.enums import AgentStatus
from app.domain.exceptions import PermissionDeniedError, ValidationError
from app.domain.models import AgentDefinition, AgentToolConfig
from app.services.agent_orchestration_types import (
    ExecutionGuard,
    OrchestrationPauseError,
    PauseCode,
    PlannerProtocolError,
    PreparedTool,
)
from app.services.agent_orchestration_validation import (
    READ_ONLY_HANDLER_REFS,
    validateToolArguments,
)
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.agent_tool_config_registry import AgentToolConfigRegistry
from app.services.agent_tool_types import AgentTool, AgentToolContext, ToolResult

_BUILTIN_HANDLER_KIND = "BUILTIN"

MSG_ORCH_AGENT_UNAVAILABLE = "编排运行所需的 Agent 或工具当前不可用"
MSG_ORCH_PERMISSION_DENIED = "当前 Agent 无权调用该工具"


class AgentOrchestrationToolExecutor:
    """Prepare/invoke split around the current tool binding and Agent ACL."""

    def __init__(
        self,
        executionGuard: ExecutionGuard,
        toolRegistry: AgentToolConfigRegistry,
        runtime: AgentRuntimeService,
    ) -> None:
        self._guard = executionGuard
        self._registry = toolRegistry
        self._runtime = runtime

    async def prepare(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
        toolName: str,
        args: dict[str, Any],
        leaseOwner: str,
        now: datetime,
    ) -> PreparedTool:
        """Run every pre-execution check and return an immutable plan."""
        await self._guard.assertExecutionAllowed(session, run, leaseOwner, now)
        agent = await self._loadActiveAgent(session, run)
        profile = await self._loadEnabledProfile(session, agent)
        self._assertSnapshotBinding(run, toolName)
        await self._loadEnabledBinding(session, profile, toolName)
        config = await self._loadEnabledToolConfig(session, toolName)
        self._assertReadOnlyBuiltin(config)
        validatedArgs = self._validateArgs(config, toolName, args)
        tool = await self._resolveTool(session, toolName)
        self._enforceAcl(agent, tool)
        return PreparedTool(tool=tool, args=dict(validatedArgs))

    async def invoke(
        self,
        session: AsyncSession,
        prepared: PreparedTool,
        context: AgentToolContext,
    ) -> ToolResult:
        """Execute a prepared tool (the only handler call site)."""
        return await prepared.tool.handler(session, dict(prepared.args), context)

    # ------------------------------------------------------------------
    # current-state loads
    # ------------------------------------------------------------------

    async def _loadActiveAgent(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
    ) -> AgentDefinition:
        stmt = select(AgentDefinition)
        if run.agent_id is not None:
            stmt = stmt.where(AgentDefinition.id == run.agent_id)
        else:
            stmt = stmt.where(AgentDefinition.agent_code == run.agent_code)
        agent = (await session.execute(stmt)).scalar_one_or_none()
        if agent is None or agent.status != AgentStatus.ACTIVE.value:
            raise _unavailable()
        return agent

    async def _loadEnabledProfile(
        self,
        session: AsyncSession,
        agent: AgentDefinition,
    ) -> AgentOrchestrationProfile:
        profile = (
            await session.execute(
                select(AgentOrchestrationProfile).where(
                    AgentOrchestrationProfile.agent_id == agent.id
                )
            )
        ).scalar_one_or_none()
        if profile is None or not profile.enabled:
            raise _unavailable()
        return profile

    async def _loadEnabledBinding(
        self,
        session: AsyncSession,
        profile: AgentOrchestrationProfile,
        toolName: str,
    ) -> AgentOrchestrationBinding:
        binding = (
            await session.execute(
                select(AgentOrchestrationBinding).where(
                    AgentOrchestrationBinding.profile_id == profile.id,
                    AgentOrchestrationBinding.tool_name == toolName,
                )
            )
        ).scalar_one_or_none()
        if binding is None or not binding.enabled:
            raise _unavailable()
        return binding

    async def _loadEnabledToolConfig(
        self,
        session: AsyncSession,
        toolName: str,
    ) -> AgentToolConfig:
        config = (
            await session.execute(
                select(AgentToolConfig).where(AgentToolConfig.name == toolName)
            )
        ).scalar_one_or_none()
        if config is None or not config.enabled:
            raise _unavailable()
        return config

    # ------------------------------------------------------------------
    # validation / policy
    # ------------------------------------------------------------------

    def _assertSnapshotBinding(
        self,
        run: AgentOrchestrationRun,
        toolName: str,
    ) -> None:
        """The Planner may only call tools frozen into this run's snapshot."""
        snapshot = run.tool_bindings_snapshot or []
        names = {
            entry.get("toolName")
            for entry in snapshot
            if isinstance(entry, dict)
        }
        if toolName not in names:
            raise PlannerProtocolError(
                f"tool {toolName!r} is not bound to this run"
            )

    def _assertReadOnlyBuiltin(self, config: AgentToolConfig) -> None:
        if (
            config.handler_kind != _BUILTIN_HANDLER_KIND
            or config.handler_ref not in READ_ONLY_HANDLER_REFS
        ):
            raise _unavailable()

    def _validateArgs(
        self,
        config: AgentToolConfig,
        toolName: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return validateToolArguments(config.input_schema or {}, args)
        except ValidationError as exc:
            message = f"tool {toolName!r} arguments rejected: {exc.message}"
            if exc.detail:
                message = f"{message} ({exc.detail})"
            raise PlannerProtocolError(message) from exc

    async def _resolveTool(
        self,
        session: AsyncSession,
        toolName: str,
    ) -> AgentTool:
        tool = self._registry.get(toolName)
        if tool is None:
            await self._registry.reload_one(session, toolName)
            tool = self._registry.get(toolName)
        if tool is None:
            raise _unavailable()
        return tool

    def _enforceAcl(self, agent: AgentDefinition, tool: AgentTool) -> None:
        try:
            self._runtime.enforcePolicies(agent, tool)
        except PermissionDeniedError as exc:
            # Generic message only: exception text names the data object/layer
            # and would leak policy internals into the Planner feedback.
            raise OrchestrationPauseError(
                PauseCode.PERMISSION_DENIED.value,
                MSG_ORCH_PERMISSION_DENIED,
            ) from exc


def _unavailable() -> OrchestrationPauseError:
    return OrchestrationPauseError(
        PauseCode.AGENT_UNAVAILABLE.value,
        MSG_ORCH_AGENT_UNAVAILABLE,
    )
