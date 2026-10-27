"""Task 2 DTO and shared-contract tests (general Agent orchestration).

Covers camelCase aliases, request-model ``extra="forbid"`` boundaries, the
brief-mandated lower bounds (independent of the ``Settings`` ceilings) and all
three Planner action shapes.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from app.domain.agent_orchestration_schemas import (
    AgentOrchestrationBindingRead,
    AgentOrchestrationProfileCreate,
    AgentOrchestrationProfileRead,
    AgentOrchestrationProfileUpdate,
    AgentOrchestrationToggle,
    AgentOrchestrationToolOptionRead,
    AgentRunCreate,
    AgentRunCreated,
    AgentRunLimits,
    AgentRunRead,
    AgentRunResume,
    AgentRunStepRead,
)
from app.services.agent_orchestration_types import (
    AskUserAction,
    CallStatus,
    CallToolAction,
    CostSource,
    FinishAction,
    OrchestrationPauseError,
    PauseCode,
    PlannerAction,
    PlannerProtocolError,
    RunStatus,
    StepKind,
    StepStatus,
)


def test_run_create_requires_explicit_model_and_camel_aliases():
    dto = AgentRunCreate.model_validate({
        "agentCode": "A", "input": "analyze", "modelConfigId": 12,
        "limits": {"maxIterations": 3, "maxToolCalls": 4,
                   "maxCostUsd": 0.2, "timeoutSeconds": 60},
    })
    assert dto.model_config_id == 12
    assert dto.limits.max_iterations == 3
    with pytest.raises(ValidationError):
        AgentRunCreate.model_validate({"agentCode": "A", "input": "x"})


def test_run_limits_reject_zero_or_negative_values():
    with pytest.raises(ValidationError):
        AgentRunLimits(maxIterations=0)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"maxIterations": 0}, "maxIterations"),
        ({"maxToolCalls": 0}, "maxToolCalls"),
        ({"timeoutSeconds": 0}, "timeoutSeconds"),
        ({"timeoutSeconds": -1}, "timeoutSeconds"),
        ({"maxCostUsd": -0.01}, "maxCostUsd"),
    ],
)
def test_run_limits_enforce_lower_bounds(payload, field):
    with pytest.raises(ValidationError) as exc_info:
        AgentRunLimits(**payload)
    assert field in str(exc_info.value)


def test_run_limits_allow_partial_overrides_and_lower_bounds_are_not_system_ceilings():
    limits = AgentRunLimits(maxIterations=1, maxToolCalls=1, timeoutSeconds=1, maxCostUsd=0)
    assert limits.max_iterations == 1
    assert limits.max_tool_calls == 1
    assert limits.timeout_seconds == 1
    assert limits.max_cost_usd == 0
    assert limits.model_dump(exclude_none=True) == {
        "max_iterations": 1,
        "max_tool_calls": 1,
        "max_cost_usd": 0,
        "timeout_seconds": 1,
    }


def test_run_create_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AgentRunCreate.model_validate({
            "agentCode": "A",
            "input": "x",
            "modelConfigId": 12,
            "enabled": True,
        })


def test_run_create_input_is_bounded_to_20000_chars():
    AgentRunCreate.model_validate({"agentCode": "A", "input": "x" * 20000, "modelConfigId": 1})
    with pytest.raises(ValidationError):
        AgentRunCreate.model_validate({"agentCode": "A", "input": "x" * 20001, "modelConfigId": 1})


def test_run_resume_bounds_and_camel_aliases():
    dto = AgentRunResume.model_validate({
        "input": "全年",
        "additionalIterations": 2,
        "additionalToolCalls": 1,
        "additionalCostUsd": 0,
        "additionalTimeoutSeconds": 60,
    })
    assert dto.additional_iterations == 2
    assert dto.additional_cost_usd == 0
    with pytest.raises(ValidationError):
        AgentRunResume.model_validate({"additionalIterations": 0})
    with pytest.raises(ValidationError):
        AgentRunResume.model_validate({"input": "x" * 20001})


def test_profile_create_accepts_design_payload_and_forbids_extras():
    dto = AgentOrchestrationProfileCreate.model_validate({
        "agentCode": "SUPPLIER_ANALYSIS_AGENT",
        "instructions": "read only",
        "toolNames": ["supplier_360", "wiki_search"],
        "defaultMaxIterations": 8,
        "defaultMaxToolCalls": 10,
        "defaultMaxCostUsd": 0.5,
        "defaultTimeoutSeconds": 180,
        "maxObservationChars": 12000,
    })
    assert dto.agent_code == "SUPPLIER_ANALYSIS_AGENT"
    assert dto.tool_names == ["supplier_360", "wiki_search"]
    assert dto.max_observation_chars == 12000
    with pytest.raises(ValidationError):
        AgentOrchestrationProfileCreate.model_validate({
            "agentCode": "A",
            "instructions": "x",
            "toolNames": [],
            "enabled": True,
        })


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"instructions": "x" * 10001}, "instructions"),
        ({"agentCode": "A" * 65, "instructions": "x"}, "agentCode"),
        ({"agentCode": "A", "instructions": "x", "maxObservationChars": 999}, "maxObservationChars"),
        ({"agentCode": "A", "instructions": "x", "maxObservationChars": 50001}, "maxObservationChars"),
        ({"agentCode": "A", "instructions": "x", "defaultMaxIterations": 0}, "defaultMaxIterations"),
        ({"agentCode": "A", "instructions": "x", "defaultMaxCostUsd": -1}, "defaultMaxCostUsd"),
    ],
)
def test_profile_create_text_and_numeric_bounds(payload, field):
    with pytest.raises(ValidationError) as exc_info:
        AgentOrchestrationProfileCreate.model_validate({
            "agentCode": "A", "instructions": "x", "toolNames": [], **payload,
        })
    assert field in str(exc_info.value)


def test_profile_create_observation_chars_boundary_values_are_allowed():
    low = AgentOrchestrationProfileCreate.model_validate({
        "agentCode": "A", "instructions": "x", "toolNames": [], "maxObservationChars": 1000,
    })
    high = AgentOrchestrationProfileCreate.model_validate({
        "agentCode": "A", "instructions": "x", "toolNames": [], "maxObservationChars": 50000,
    })
    assert low.max_observation_chars == 1000
    assert high.max_observation_chars == 50000


def test_profile_update_is_a_full_update_with_revision_and_no_agent_code():
    full = {
        "instructions": "new",
        "toolNames": ["wiki_read"],
        "defaultMaxIterations": 8,
        "defaultMaxToolCalls": 10,
        "defaultMaxCostUsd": 0.5,
        "defaultTimeoutSeconds": 180,
        "maxObservationChars": 12000,
        "revision": 3,
    }
    dto = AgentOrchestrationProfileUpdate.model_validate(full)
    assert dto.revision == 3
    assert dto.default_max_tool_calls == 10
    with pytest.raises(ValidationError):
        AgentOrchestrationProfileUpdate.model_validate({
            "instructions": "x", "toolNames": ["wiki_read"], "revision": 1,
        })
    with pytest.raises(ValidationError):
        AgentOrchestrationProfileUpdate.model_validate({**full, "revision": 0})
    with pytest.raises(ValidationError):
        AgentOrchestrationProfileUpdate.model_validate({**full, "agentCode": "A"})


def test_toggle_requires_explicit_enabled_and_revision():
    dto = AgentOrchestrationToggle.model_validate({"enabled": True, "revision": 2})
    assert dto.enabled is True
    assert dto.revision == 2
    with pytest.raises(ValidationError):
        AgentOrchestrationToggle.model_validate({"revision": 1})
    with pytest.raises(ValidationError):
        AgentOrchestrationToggle.model_validate({"enabled": True, "revision": 0})


def test_binding_and_tool_option_reads_use_camel_case_aliases():
    binding = AgentOrchestrationBindingRead.model_validate({
        "toolName": "wiki_search", "enabled": True, "sortOrder": 2,
    })
    option = AgentOrchestrationToolOptionRead.model_validate({
        "toolName": "wiki_search",
        "description": "search",
        "inputSchema": {"type": "object", "properties": {}},
    })
    assert binding.model_dump(by_alias=True) == {
        "toolName": "wiki_search", "enabled": True, "sortOrder": 2,
    }
    assert option.model_dump(by_alias=True)["inputSchema"]["type"] == "object"
    assert "handlerRef" not in option.model_dump(by_alias=True)


def test_profile_read_serializes_ordered_bindings_and_aliases():
    profile = AgentOrchestrationProfileRead(
        agentCode="A",
        instructions="x",
        enabled=False,
        defaultMaxIterations=8,
        defaultMaxToolCalls=10,
        defaultMaxCostUsd=0.5,
        defaultTimeoutSeconds=180,
        maxObservationChars=12000,
        revision=1,
        bindings=[
            AgentOrchestrationBindingRead(toolName="wiki_search", enabled=True, sortOrder=0),
        ],
    )
    payload = profile.model_dump(by_alias=True)
    assert payload["agentCode"] == "A"
    assert payload["defaultMaxCostUsd"] == 0.5
    assert payload["bindings"] == [{"toolName": "wiki_search", "enabled": True, "sortOrder": 0}]


def test_run_created_returns_public_run_id_and_status():
    created = AgentRunCreated(runId="01JABC", status=RunStatus.QUEUED)
    assert created.model_dump(by_alias=True) == {"runId": "01JABC", "status": "QUEUED"}


def test_run_read_includes_budgets_counters_snapshots_and_camel_aliases():
    run = AgentRunRead(
        runId="01JABC",
        agentCode="A",
        question="q",
        createdBy="alice",
        modelConfigId=12,
        status=RunStatus.PAUSED,
        instructionsSnapshot="snapshot",
        toolBindingsSnapshot=[{"toolName": "wiki_search"}],
        maxIterations=8,
        maxToolCalls=10,
        maxCostUsd=0.5,
        timeoutSeconds=180,
        iterationCount=2,
        toolCallCount=3,
        promptTokens=10,
        completionTokens=5,
        totalTokens=15,
        cost=0.01,
        costSource=CostSource.MIXED,
        costComplete=False,
        finalAnswer=None,
        pauseCode=PauseCode.NEEDS_INPUT,
        pauseDetail={"missing": "period"},
        errorCode=None,
        errorMessage=None,
        cancelRequested=False,
        createdTime="2026-10-26T00:00:00Z",
        updatedTime="2026-10-26T00:00:00Z",
        queuedAt="2026-10-26T00:00:00Z",
        startedAt=None,
        finishedAt=None,
        revision=2,
    )
    payload = run.model_dump(by_alias=True)
    assert payload["runId"] == "01JABC"
    assert payload["modelConfigId"] == 12
    assert payload["costSource"] == "MIXED"
    assert payload["costComplete"] is False
    assert payload["pauseCode"] == "NEEDS_INPUT"
    assert payload["pauseDetail"] == {"missing": "period"}
    assert payload["toolBindingsSnapshot"] == [{"toolName": "wiki_search"}]
    assert payload["cancelRequested"] is False


def test_run_step_read_exposes_trace_fields():
    step = AgentRunStepRead(
        stepNo=0,
        iterationNo=1,
        kind=StepKind.TOOL,
        status=StepStatus.SUCCEEDED,
        action="CALL_TOOL",
        planSummary="lookup",
        toolName="wiki_search",
        inputJson={"query": "risk"},
        outputJson={"text": "{}"},
        answer=None,
        errorCode=None,
        errorMessage=None,
        promptTokens=1,
        completionTokens=2,
        cost=0.0,
        startedAt="2026-10-26T00:00:00Z",
        finishedAt="2026-10-26T00:00:01Z",
    )
    payload = step.model_dump(by_alias=True)
    assert payload["stepNo"] == 0
    assert payload["kind"] == "TOOL"
    assert payload["inputJson"] == {"query": "risk"}


def test_shared_contract_enums_match_database_whitelists():
    assert {member.value for member in RunStatus} == {
        "QUEUED", "RUNNING", "PAUSED", "SUCCEEDED", "FAILED", "CANCELLED",
    }
    assert {member.value for member in StepKind} == {"PLAN", "TOOL"}
    assert {member.value for member in StepStatus} == {
        "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED",
    }
    assert {member.value for member in CostSource} == {"MEASURED", "ESTIMATED", "UNKNOWN", "MIXED"}
    assert {member.value for member in CallStatus} == {"SUCCEEDED", "OUTPUT_INVALID", "FAILED"}
    assert {member.value for member in PauseCode} == {
        "NEEDS_INPUT", "AMBIGUOUS", "PERMISSION_DENIED", "BUDGET_EXHAUSTED",
        "TOOL_FAILED", "PLANNER_OUTPUT_INVALID", "AGENT_UNAVAILABLE",
        "INFRASTRUCTURE_ERROR",
    }


def test_all_planner_action_shapes_are_frozen_and_typed():
    call = CallToolAction(toolName="wiki_search", args={"query": "risk"}, summary="lookup")
    ask = AskUserAction(question="which year?", reason="ambiguous")
    finish = FinishAction(answer="done")
    assert call.toolName == "wiki_search"
    assert ask.question == "which year?"
    assert finish.answer == "done"
    for action, field in ((call, "toolName"), (ask, "question"), (finish, "answer")):
        assert dataclasses.is_dataclass(action)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(action, field, "mutated")
    planner_action: PlannerAction = call
    assert planner_action is call


def test_pause_and_protocol_errors_carry_structured_detail():
    error = OrchestrationPauseError("NEEDS_INPUT", "need input", {"field": "period"})
    assert error.code == "NEEDS_INPUT"
    assert error.message == "need input"
    assert error.detail == {"field": "period"}
    assert OrchestrationPauseError("x", "y").detail == {}
    assert isinstance(PlannerProtocolError("bad"), Exception)
