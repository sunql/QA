"""Shared contracts for the general Agent orchestration subsystem.

The declarations here are normative for later tasks (Planner, executor, runner,
metering, API). They deliberately depend only on types that already exist so
early modules never import Store/Runner/Metering modules that are built later;
constructor injection supplies the real implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.services.agent_tool_types import AgentTool

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.agent_orchestration_models import AgentOrchestrationRun


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepKind(StrEnum):
    PLAN = "PLAN"
    TOOL = "TOOL"


class StepStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CostSource(StrEnum):
    """Run-level source includes ``MIXED``; usage rows never use it."""

    MEASURED = "MEASURED"
    ESTIMATED = "ESTIMATED"
    UNKNOWN = "UNKNOWN"
    MIXED = "MIXED"


class CallStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    FAILED = "FAILED"


class PauseCode(StrEnum):
    NEEDS_INPUT = "NEEDS_INPUT"
    AMBIGUOUS = "AMBIGUOUS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TOOL_FAILED = "TOOL_FAILED"
    PLANNER_OUTPUT_INVALID = "PLANNER_OUTPUT_INVALID"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"


@dataclass(frozen=True)
class CallToolAction:
    toolName: str
    args: dict[str, Any]
    summary: str


@dataclass(frozen=True)
class AskUserAction:
    question: str
    reason: str


@dataclass(frozen=True)
class FinishAction:
    answer: str


PlannerAction = CallToolAction | AskUserAction | FinishAction


@dataclass(frozen=True)
class PreparedTool:
    tool: AgentTool
    args: dict[str, Any]


@dataclass(frozen=True)
class MeteredCompletion:
    response: LlmResponse
    usageId: int


class OrchestrationPauseError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


class LeaseLostError(Exception):
    """控制流异常：租约已丢失或 run 已被取消。

    Runner 捕获后必须立即停止当前 run 的工作且**不写入任何状态**。它刻意不继承
    ``OrchestrationPauseError``：租约丢失是 Worker 侧并发事实，绝不能变成用户
    可见的暂停（也不该触发 Planner 纠正）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlannerProtocolError(Exception):
    pass


class ExecutionGuard(Protocol):
    async def assertExecutionAllowed(
        self,
        session: AsyncSession,
        run: AgentOrchestrationRun,
        leaseOwner: str,
        now: datetime,
    ) -> None: ...


class ClaimedRunRunner(Protocol):
    async def runClaimed(self, runId: int, workerId: str) -> None: ...


class PlanningLlmClient(Protocol):
    async def completeForPlanning(
        self, messages: list[LlmMessage]
    ) -> MeteredCompletion: ...

    async def markOutputInvalid(self, usageId: int) -> None: ...
