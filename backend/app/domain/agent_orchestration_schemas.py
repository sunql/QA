"""REST DTOs for the general Agent orchestration API (Task 2).

Conventions match the rest of the codebase:
- Python fields are ``snake_case``; JSON is ``camelCase`` via ``CamelModel``.
- Every *request* model sets ``extra="forbid"`` so unknown keys fail at the
  boundary instead of being silently ignored.
- Numeric lower bounds are enforced here; system ceilings are deliberately
  **not** duplicated in DTOs (services validate them against ``Settings``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.domain.schemas import CamelModel
from app.services.agent_orchestration_types import (
    CostSource,
    PauseCode,
    RunStatus,
    StepKind,
    StepStatus,
)

StepAction = Literal["CALL_TOOL", "FINISH", "ASK_USER"]

AGENT_CODE_MAX_LENGTH = 64
INSTRUCTIONS_MAX_LENGTH = 10000
RUN_INPUT_MAX_LENGTH = 20000
OBSERVATION_CHARS_MIN = 1000
OBSERVATION_CHARS_MAX = 50000


# ---------------------------------------------------------------------------
# Configuration DTOs
# ---------------------------------------------------------------------------


class AgentOrchestrationProfileCreate(CamelModel):
    """``POST /agent-orchestrations`` request.

    ``enabled`` is intentionally absent: profiles are always created disabled
    and require an explicit toggle.
    """

    model_config = ConfigDict(extra="forbid")

    agent_code: str = Field(min_length=1, max_length=AGENT_CODE_MAX_LENGTH)
    instructions: str = Field(min_length=1, max_length=INSTRUCTIONS_MAX_LENGTH)
    tool_names: list[str] = Field(default_factory=list)
    default_max_iterations: int = Field(default=20, ge=1)
    default_max_tool_calls: int = Field(default=30, ge=1)
    default_max_cost_usd: float = Field(default=5.0, ge=0)
    default_timeout_seconds: int = Field(default=900, ge=1)
    max_observation_chars: int = Field(
        default=OBSERVATION_CHARS_MAX,
        ge=OBSERVATION_CHARS_MIN,
        le=OBSERVATION_CHARS_MAX,
    )


class AgentOrchestrationProfileUpdate(CamelModel):
    """``PUT /agent-orchestrations/{agentCode}`` request (full update)."""

    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(min_length=1, max_length=INSTRUCTIONS_MAX_LENGTH)
    tool_names: list[str]
    default_max_iterations: int = Field(ge=1)
    default_max_tool_calls: int = Field(ge=1)
    default_max_cost_usd: float = Field(ge=0)
    default_timeout_seconds: int = Field(ge=1)
    max_observation_chars: int = Field(
        ge=OBSERVATION_CHARS_MIN,
        le=OBSERVATION_CHARS_MAX,
    )
    revision: int = Field(ge=1)


class AgentOrchestrationToggle(CamelModel):
    """``POST /agent-orchestrations/{agentCode}/toggle`` request."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    revision: int = Field(ge=1)


class AgentOrchestrationBindingRead(CamelModel):
    tool_name: str
    enabled: bool
    sort_order: int


class AgentOrchestrationProfileRead(CamelModel):
    agent_code: str
    instructions: str
    enabled: bool
    default_max_iterations: int
    default_max_tool_calls: int
    default_max_cost_usd: float
    default_timeout_seconds: int
    max_observation_chars: int
    revision: int
    bindings: list[AgentOrchestrationBindingRead] = Field(default_factory=list)


class AgentOrchestrationToolOptionRead(CamelModel):
    """Whitelisted tool option; deliberately never exposes ``handlerRef``."""

    tool_name: str
    description: str
    input_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Run DTOs
# ---------------------------------------------------------------------------


class AgentRunLimits(CamelModel):
    """Per-run budget overrides; omitted fields fall back to profile defaults."""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    timeout_seconds: int | None = Field(default=None, ge=1)


class AgentRunCreate(CamelModel):
    """``POST /agent-runs`` request."""

    model_config = ConfigDict(extra="forbid")

    agent_code: str = Field(min_length=1, max_length=AGENT_CODE_MAX_LENGTH)
    input: str = Field(min_length=1, max_length=RUN_INPUT_MAX_LENGTH)
    model_config_id: int = Field(ge=1)
    limits: AgentRunLimits | None = None


class AgentRunResume(CamelModel):
    """``POST /agent-runs/{runId}/resume`` request.

    Which increments are required depends on the persisted pause code and is
    enforced by the run service, not by this DTO.
    """

    model_config = ConfigDict(extra="forbid")

    input: str | None = Field(default=None, min_length=1, max_length=RUN_INPUT_MAX_LENGTH)
    additional_iterations: int | None = Field(default=None, ge=1)
    additional_tool_calls: int | None = Field(default=None, ge=1)
    additional_cost_usd: float | None = Field(default=None, ge=0)
    additional_timeout_seconds: int | None = Field(default=None, ge=1)


class AgentRunCreated(CamelModel):
    run_id: str
    status: RunStatus


class AgentRunRead(CamelModel):
    run_id: str
    agent_code: str
    question: str
    created_by: str
    model_config_id: int
    status: RunStatus
    instructions_snapshot: str
    tool_bindings_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    max_iterations: int
    max_tool_calls: int
    max_cost_usd: float
    timeout_seconds: int
    iteration_count: int
    tool_call_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float
    cost_source: CostSource
    cost_complete: bool
    final_answer: str | None = None
    pause_code: PauseCode | None = None
    pause_detail: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    created_time: datetime
    updated_time: datetime
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    revision: int


class AgentRunStepRead(CamelModel):
    step_no: int
    iteration_no: int
    kind: StepKind
    status: StepStatus
    action: StepAction | None = None
    plan_summary: str | None = None
    tool_name: str | None = None
    input_json: dict[str, Any] | None = None
    output_json: dict[str, Any] | None = None
    answer: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    prompt_tokens: int
    completion_tokens: int
    cost: float
    started_at: datetime
    finished_at: datetime | None = None
