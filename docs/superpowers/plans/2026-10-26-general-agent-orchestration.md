# General Agent Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independently deployed, read-only, multi-tool Agent orchestration capability with persisted runs, steps and per-call LLM usage, while preserving every existing Agent, Chat, L4, scheduler and research behavior.

**Architecture:** Add a 1:1 orchestration profile beside the existing `AgentDefinition`, a separate Run/Step/Usage persistence model, a single-action JSON Planner, and a PostgreSQL-claimed Worker. Only `POST /api/v1/agent-runs` enters this layer; the legacy run endpoint remains physically on `AgentRuntimeService.run`. The feature and its Worker are off by default, and menu rows are seeded only during explicit gray rollout.

**Tech Stack:** Python 3.12+, FastAPI, SQLAlchemy 2 async, Alembic, PostgreSQL, Pydantic 2, `jsonschema` 2020-12, pytest/pytest-asyncio, React 19, TypeScript, Ant Design, Vitest, Docker Compose.

## Global Constraints

- The normative design is `docs/superpowers/specs/2026-10-26-general-agent-orchestration-design.md`; this plan resolves implementation details but does not widen its scope.
- Existing `POST /api/v1/agents/{code}/run` must always execute `AgentRuntimeService.run`; never route it to the new runner, including when an orchestration profile exists.
- Do not alter existing Agent DTOs, `/agents/options`, `runnable`, Chat, L4, scheduler, research, or the six existing tables listed in spec §2.2.
- Only the seven handler refs `supplier_360`, `supplier_risk`, `graph_traverse`, `wiki_search`, `wiki_read`, `rule_evaluate`, `coverage_status` are orchestration-eligible. `nl2sql_default` and L4 `execute_sql` are forbidden.
- One Planner response yields exactly one of `CALL_TOOL`, `FINISH`, `ASK_USER`; tools execute sequentially, never in parallel.
- `ENABLE_AGENT_ORCHESTRATION=false` by default. Disabled means no new routers and no Worker; never use FastAPI `BackgroundTasks`.
- Every LLM response or failure must be metered immediately. Unknown usage is `UNKNOWN` with `costComplete=false`; never present it as measured or complete.
- No automatic model rerouting. Planner and tool-internal LLM calls use the run's exact `model_config_id`.
- ACL is rechecked before every tool call through a pure delegate to the existing `_enforcePolicies`; permission denial pauses without feeding bypass details to the LLM.
- Tool observations are untrusted, JSON-safe, size-bounded, and wrapped with the existing prompt sanitizer.
- ORM persistence may mutate rows; domain inputs, Planner actions, prepared tools and observations use frozen dataclasses or immutable copies.
- Python functions/variables use `camelCase`; ORM and Pydantic fields use `snake_case`; JSON uses `camelCase`. TypeScript uses `camelCase` and explicit exported types.
- Functions are under 50 lines, nesting is at most four levels, and every file remains below 800 lines.
- Follow RED → GREEN → REFACTOR. New/changed code coverage is at least 80%. Do not weaken a failing test to conceal an implementation defect.
- Backend integration tests use the real `TEST_DATABASE_URL` PostgreSQL database, Alembic migrations and the complete API. Never substitute SQLite or direct-service-only tests for integration coverage.
- Before every test run, confirm the intended test database and a clean intended worktree. Never print `.env`; never run destructive fixtures against a business database; never SIGKILL pytest.
- Production operations are performed manually by the user. This feature delivers artifacts and a runbook only; it does not execute production deployment.
- Commit only when the user has authorized implementation commits under the execution workflow. Use the listed conventional-commit commands and no attribution lines.

## File Structure and Responsibilities

### New backend files

| File | Responsibility |
|---|---|
| `backend/alembic/versions/0135_agent_orchestration.py` | Add five tables, constraints and indexes only |
| `backend/app/domain/agent_orchestration_models.py` | Profile/Binding/Run/Step/Usage ORM |
| `backend/app/domain/agent_orchestration_schemas.py` | New CamelModel request/response contracts |
| `backend/app/services/agent_orchestration_types.py` | Status enums, frozen Planner actions, pause/protocol exceptions, limits |
| `backend/app/services/agent_orchestration_validation.py` | Seven-handler whitelist, object-schema and argument validation, bounded observations |
| `backend/app/services/agent_orchestration_config_service.py` | Profile/binding CRUD, management ACL, revision and audit |
| `backend/app/services/agent_orchestration_run_service.py` | Create/list/get/resume/cancel and creator/admin read ACL |
| `backend/app/services/agent_orchestration_planner.py` | Safe prompt construction and single-action parsing |
| `backend/app/services/agent_orchestration_metering.py` | Metered client/factory, usage facts, run/step aggregates |
| `backend/app/services/agent_orchestration_tool_executor.py` | Current configuration, whitelist, schema, ACL and handler invocation |
| `backend/app/services/agent_orchestration_store.py` | Lease-checked step creation/completion and run transitions |
| `backend/app/services/agent_orchestration_runner.py` | Bounded plan–execute–observe loop and recovery |
| `backend/app/workers/agent_orchestration_worker.py` | Atomic claim, lease, polling and graceful shutdown |
| `backend/app/api/v1/agent_orchestrations.py` | Configuration API |
| `backend/app/api/v1/agent_runs.py` | Run/Step/Resume/Cancel API |
| `backend/scripts/seed_agent_orchestration_menu.py` | Explicit gray-rollout-only menu seed |

### Minimal existing backend changes

| File | Additive change |
|---|---|
| `backend/app/domain/models.py` | Import new ORM module at file end for metadata registration; no existing class changes |
| `backend/app/config.py` | Feature flag, poll/lease/retry limits and hard budget ceilings |
| `backend/app/main.py` | Register new routers only when enabled |
| `backend/app/tests/_testapp.py` | Mirror the same conditional router registration |
| `backend/app/services/agent_runtime_service.py` | Add `enforcePolicies` delegate; preserve `_enforcePolicies` and `run` |
| `backend/app/services/agent_tool_config_service.py` | Reject deletion only when a new binding references the tool |
| `backend/app/services/chat_usage.py` | Expose the existing cost formula through a public pure delegate, without changing its math |
| `backend/pyproject.toml`, `backend/uv.lock` | Make existing transitive `jsonschema` an explicit runtime dependency |
| `docker/docker-compose.yml`, `docker/docker-compose.centos.yml` | Add an opt-in `agent-orchestration-worker` profile |

### New frontend files

| File | Responsibility |
|---|---|
| `frontend/src/types/agentOrchestration.ts` | API contracts and unions |
| `frontend/src/api/agentOrchestrationApi.ts` | Configuration and run HTTP functions |
| `frontend/src/pages/AgentOrchestrationPage.tsx` | Profile, bindings and default budgets |
| `frontend/src/pages/AgentTaskPage.tsx` | Create, poll, resume and cancel tasks |
| `frontend/src/components/agent/AgentRunTimeline.tsx` | Incremental PLAN/TOOL/final-answer display |

Only add routes in `App.tsx` and additive keys in `zh-CN.ts`/`en-US.ts`. Do not edit `ChatPage`, `ChatStore`, `AgentRuntimePage`, or old Agent API modules.

## Shared Contracts Used Across Tasks

These declarations are normative; later tasks must use these exact names.

```python
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
```

`ExecutionGuard`, `ClaimedRunRunner` and `PlanningLlmClient` live in `agent_orchestration_types.py` and use `typing.Protocol` plus `TYPE_CHECKING` imports for ORM/session types. They prevent early tasks from importing Store/Runner/Metering modules that are implemented later; constructor injection supplies the real objects in Tasks 6–9.

Run `cost_source` and `cost_complete` are persisted columns even though spec §4.3's field table omitted them; they are required by spec §6.3 and the run API. `cost_complete` is true only when no usage row is `UNKNOWN`.

Every successful claim starts a fresh active-execution time window by setting `started_at`. Resume also adds `additionalTimeoutSeconds` to `timeout_seconds` and clears `started_at` while queued; expired-lease recovery clears and resets the same timestamp. Token, cost, iteration and tool-call totals remain cumulative. This prevents paused time or a dead Worker's expired window from consuming the next execution window.

### Test environment preflight

Run these commands without printing credentials:

```bash
cd backend
uv run python -c 'import os; from sqlalchemy.engine import make_url; u=make_url(os.environ["TEST_DATABASE_URL"]); assert u.get_backend_name()=="postgresql"; assert u.database and "test" in u.database; assert os.environ.get("TEST_NEO4J_URI"); print("isolated test environment configured")'
git status --short
```

All following backend test commands inherit this environment. Frontend commands run from `frontend/`.

---

### Task 1: Additive Schema, ORM and Safe Defaults

**Files:**
- Create: `backend/alembic/versions/0135_agent_orchestration.py`
- Create: `backend/app/domain/agent_orchestration_models.py`
- Modify: `backend/app/domain/models.py` (end-of-file import only)
- Modify: `backend/app/config.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_schema.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_config.py`

**Interfaces:**
- Consumes: existing `Base`, `BigIntPk`, `BigIntFk`, `TimestampMixin` and Alembic head `0134`.
- Produces: ORM classes `AgentOrchestrationProfile`, `AgentOrchestrationBinding`, `AgentOrchestrationRun`, `AgentOrchestrationStep`, `AgentOrchestrationUsage`; settings consumed by later tasks.

- [ ] **Step 1: Verify the actual Alembic chain and write failing schema/settings tests**

Use the Alembic API rather than assuming the newest filename:

```bash
cd backend
uv run python -c 'from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config("alembic.ini")); print("heads=", s.get_heads()); print("0134 down=", s.get_revision("0134").down_revision)'
```

Expected: exactly one head and `0134` currently descends from `0133`. If another head exists, stop and reconcile the migration lineage before assigning `down_revision`; do not invent a merge or renumber an already-shared migration.

Create the integration test:

```python
from sqlalchemy import inspect, text


async def test_orchestration_schema_is_additive(dbSession):
    tables = set(await dbSession.run_sync(lambda s: inspect(s).get_table_names()))
    expected = {
        "agent_orchestration_profile",
        "agent_orchestration_binding",
        "agent_orchestration_run",
        "agent_orchestration_step",
        "agent_orchestration_usage",
    }
    assert expected <= tables
    columns = await dbSession.run_sync(
        lambda s: {c["name"] for c in inspect(s).get_columns("agent_orchestration_run")}
    )
    assert {"run_id", "agent_code", "cost_source", "cost_complete", "lease_until"} <= columns
    fk_rows = (await dbSession.execute(text(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid='agent_orchestration_run'::regclass AND contype='f'"
    ))).scalars().all()
    assert "n" in fk_rows  # agent_id ON DELETE SET NULL


async def test_legacy_agent_tables_have_no_orchestration_columns(dbSession):
    for table in ("agent_definition", "agent_access_policy", "agent_tool_config"):
        columns = await dbSession.run_sync(
            lambda s: {c["name"] for c in inspect(s).get_columns(table)}
        )
        assert not any("orchestration" in c or c == "execution_mode" for c in columns)
```

Create the settings test with the exact defaults and aliases:

```python
from app.config import Settings


def test_orchestration_is_disabled_and_bounded_by_default():
    s = Settings(_env_file=None)
    assert s.enableAgentOrchestration is False
    assert s.agentOrchestrationPollSeconds == 2.0
    assert s.agentOrchestrationLeaseSeconds == 60
    assert s.agentOrchestrationMaxIterations == 20
    assert s.agentOrchestrationMaxToolCalls == 30
    assert s.agentOrchestrationMaxCostUsd == 5.0
    assert s.agentOrchestrationMaxTimeoutSeconds == 900
    assert s.agentOrchestrationMaxObservationChars == 50000
    assert s.agentOrchestrationMaxPlannerCorrections == 2
    assert s.agentOrchestrationMaxToolFailures == 2
```

- [ ] **Step 2: Run RED**

Run:

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_config.py -q
uv run pytest app/tests/integration/test_agent_orchestration_schema.py -q
```

Expected: settings fail on missing attributes; integration fails because the five tables are absent.

- [ ] **Step 3: Add settings and the five-table migration**

Add these fields to `Settings` in one orchestration block:

```python
enableAgentOrchestration: bool = Field(default=False, alias="ENABLE_AGENT_ORCHESTRATION")
agentOrchestrationPollSeconds: float = Field(default=2.0, alias="AGENT_ORCHESTRATION_POLL_SECONDS")
agentOrchestrationLeaseSeconds: int = Field(default=60, alias="AGENT_ORCHESTRATION_LEASE_SECONDS")
agentOrchestrationMaxIterations: int = Field(default=20, alias="AGENT_ORCHESTRATION_MAX_ITERATIONS")
agentOrchestrationMaxToolCalls: int = Field(default=30, alias="AGENT_ORCHESTRATION_MAX_TOOL_CALLS")
agentOrchestrationMaxCostUsd: float = Field(default=5.0, alias="AGENT_ORCHESTRATION_MAX_COST_USD")
agentOrchestrationMaxTimeoutSeconds: int = Field(default=900, alias="AGENT_ORCHESTRATION_MAX_TIMEOUT_SECONDS")
agentOrchestrationMaxObservationChars: int = Field(default=50000, alias="AGENT_ORCHESTRATION_MAX_OBSERVATION_CHARS")
agentOrchestrationMaxPlannerCorrections: int = Field(default=2, alias="AGENT_ORCHESTRATION_MAX_PLANNER_CORRECTIONS")
agentOrchestrationMaxToolFailures: int = Field(default=2, alias="AGENT_ORCHESTRATION_MAX_TOOL_FAILURES")
```

Use `revision="0135"` and `down_revision="0134"` after Step 1 confirms that lineage. Create tables with PostgreSQL `JSONB`, timezone timestamps and these exact relationships:

- profile: unique non-null `agent_id` FK `CASCADE`; non-null instructions, `enabled=false`, four defaults, observation limit, `revision=1`, timestamps.
- binding: non-null `profile_id` FK `CASCADE`, `tool_name`, `enabled`, `sort_order`; `UNIQUE(profile_id, tool_name)`.
- run: integer PK; unique UUID-string `run_id`; nullable `agent_id` FK `SET NULL`; immutable snapshots; creator/model/budgets; counters; token/cost fields; `cost_source="UNKNOWN"`; `cost_complete=false`; answer/pause/error/cancel/lease/revision/lifecycle fields.
- step: integer PK; integer `run_id` FK to run.id `CASCADE`; `iteration_no`, `kind`, `status`, action/plan/tool/input/output/answer/error/usage/timestamps; `UNIQUE(run_id, step_no)`.
- usage: integer PK; run FK `CASCADE`; nullable step FK `SET NULL`; caller/model/tokens/cost/source/status/request time.

Add `Index("ix_agent_orchestration_run_status_id", "status", "id")`, creator/time and agent/time indexes, plus `Index("ix_agent_orchestration_step_run_step", "run_id", "step_no")`. Add check constraints for all enum-like columns and nonnegative counters. The migration must contain no `alter_column`, no old-table create/drop and no data seed.

- [ ] **Step 4: Implement ORM mappings and register metadata**

Map every migration column with the same names and types. Use `Numeric(16, 8)` for cost budgets and `Numeric(20, 8)` for aggregate cost, `String(36)` for public run UUID, `String(32)` for status/kind/action/caller/cost source, and `Text` for question/instructions/answers/messages. Do not add relationships to existing `AgentDefinition`; query new profiles explicitly so old registry behavior and loading remain unchanged.

At the end of `models.py`, add only:

```python
from app.domain import agent_orchestration_models as _agent_orchestration_models  # noqa: E402,F401
```

- [ ] **Step 5: Run GREEN and legacy-schema checks**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_config.py app/tests/integration/test_agent_orchestration_schema.py -q
uv run pytest app/tests/integration/test_agent_runtime_api.py app/tests/integration/test_agent_scheduler_api.py -q
uv run ruff check app/domain/agent_orchestration_models.py app/config.py
```

Expected: PASS. If a scheduler test filename is not collected, replace that path with the actual file found by `rg -l "AgentScheduler" app/tests/integration`; do not treat an uncollected command as success.

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/0135_agent_orchestration.py backend/app/domain/agent_orchestration_models.py backend/app/domain/models.py backend/app/config.py backend/app/tests/integration/test_agent_orchestration_schema.py backend/app/tests/unit/test_agent_orchestration_config.py
git commit -m "feat: add isolated agent orchestration persistence"
```

---

### Task 2: API DTOs, Read-Only Whitelist and Argument/Observation Validation

**Files:**
- Create: `backend/app/domain/agent_orchestration_schemas.py`
- Create: `backend/app/services/agent_orchestration_types.py`
- Create: `backend/app/services/agent_orchestration_validation.py`
- Modify: `backend/pyproject.toml`, `backend/uv.lock`
- Test: `backend/app/tests/unit/test_agent_orchestration_schemas.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_validation.py`

**Interfaces:**
- Produces: `READ_ONLY_HANDLER_REFS`, `validateObjectSchema(schema)`, `validateToolArguments(schema,args)`, `serializeObservation(result,maxChars)`, DTOs used by APIs and frontend.

- [ ] **Step 1: Write failing DTO and security-validation tests**

Cover camelCase aliases, hard-limit-independent DTO lower bounds, and all action shapes:

```python
import pytest
from pydantic import ValidationError
from app.domain.agent_orchestration_schemas import AgentRunCreate, AgentRunLimits


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
```

Validation tests:

```python
import pytest
from app.domain.exceptions import ValidationError
from app.services.agent_tool_types import ToolResult
from app.services.agent_orchestration_validation import (
    READ_ONLY_HANDLER_REFS, serializeObservation,
    validateObjectSchema, validateToolArguments,
)


def test_only_seven_read_only_handlers_are_allowed():
    assert READ_ONLY_HANDLER_REFS == frozenset({
        "supplier_360", "supplier_risk", "graph_traverse", "wiki_search",
        "wiki_read", "rule_evaluate", "coverage_status",
    })
    assert "nl2sql_default" not in READ_ONLY_HANDLER_REFS
    assert "execute_sql" not in READ_ONLY_HANDLER_REFS


def test_unknown_arguments_are_rejected_before_handler():
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    validateObjectSchema(schema)
    validateToolArguments(schema, {"query": "risk"})
    with pytest.raises(ValidationError):
        validateToolArguments(schema, {"query": "risk", "handlerRef": "execute_sql"})


def test_observation_is_bounded_and_json_safe():
    result = ToolResult(data={"text": "abcdef"}, answer="answer")
    observed = serializeObservation(result, 24)
    assert observed["truncated"] is True
    assert observed["text"].endswith("[truncated]")
    assert len(observed["text"]) <= 24
    assert observed["original_chars"] > 24
```

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_schemas.py app/tests/unit/test_agent_orchestration_validation.py -q
```

Expected: import failures for the new modules.

- [ ] **Step 3: Declare dependencies and implement contracts**

Add `"jsonschema>=4.26,<5"` to runtime dependencies and run `uv lock` from `backend/`; the lock already contains 4.26.0 transitively, so this should make it direct without introducing a new major version.

Create `agent_orchestration_types.py` with all Shared Contracts declarations, including `RunStatus`, `StepKind`, `StepStatus`, `CostSource`, `CallStatus` and `PauseCode` `StrEnum` classes; do not add speculative fields.

Implement DTOs with `extra="forbid"` on every request model:

- `AgentOrchestrationProfileCreate/Update/Read`, `AgentOrchestrationBindingRead`, `AgentOrchestrationToolOptionRead`, `AgentOrchestrationToggle`.
- `AgentRunLimits`, `AgentRunCreate`, `AgentRunResume`, `AgentRunCreated`, `AgentRunRead`, `AgentRunStepRead`.
- Text limits: Agent code 64, instructions 10000, run input 20000, resume input 20000, answer fields read-only.
- Numeric lower bounds: iterations/tool calls/timeout ≥1, costs ≥0, observation chars 1000–50000, revision ≥1. System ceilings are enforced in services against `Settings`, not duplicated in DTOs.
- `AgentRunRead` includes `runId`, `agentCode`, `question`, `createdBy`, `modelConfigId`, all four budgets, all counters/tokens/cost, `costSource`, `costComplete`, `finalAnswer`, `pauseCode`, `pauseDetail`, `errorCode`, `errorMessage`, timestamps and revision.

Implement validation using `Draft202012Validator.check_schema`. Require root `type="object"`, a dict `properties`, no `patternProperties`, and `additionalProperties` absent or false. Recursively reject keys not listed in `properties` for object nodes and validate the full value with `iter_errors`; return a new dict copy. Raise `ValidationError` with field paths but never echo argument values.

Implement observation as:

```python
safe = jsonSafe({"data": result.data, "answer": result.answer})
raw = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
marker = "[truncated]"
text = raw if len(raw) <= maxChars else raw[: max(0, maxChars-len(marker))] + marker
return {"text": text, "truncated": len(raw) > maxChars, "original_chars": len(raw)}
```

Return a fresh dict and use the canonical `app.utils.json_safe.jsonSafe` only.

- [ ] **Step 4: Run GREEN and malformed-schema cases**

Add parameterized cases for non-object roots, invalid schema types, nested unknown keys, non-dict args and `additionalProperties: {"type":"string"}`. Each must fail before any handler can be invoked.

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_schemas.py app/tests/unit/test_agent_orchestration_validation.py -q
uv run ruff check app/domain/agent_orchestration_schemas.py app/services/agent_orchestration_types.py app/services/agent_orchestration_validation.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/app/domain/agent_orchestration_schemas.py backend/app/services/agent_orchestration_types.py backend/app/services/agent_orchestration_validation.py backend/app/tests/unit/test_agent_orchestration_schemas.py backend/app/tests/unit/test_agent_orchestration_validation.py
git commit -m "feat: define orchestration contracts and read-only validation"
```

---

### Task 3: Profile Configuration Service and Feature-Flagged API

**Files:**
- Create: `backend/app/services/agent_orchestration_config_service.py`
- Create: `backend/app/api/v1/agent_orchestrations.py`
- Modify: `backend/app/main.py`, `backend/app/tests/_testapp.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_config_api.py`

**Interfaces:**
- Consumes: DTOs, ORM, `AclService.assertCanModify`, `ModelConfigService`-style domain errors, existing `OutboxService`.
- Produces: `AgentOrchestrationConfigService.create/get/list/listToolOptions/update/toggle`; five profile endpoints plus `GET /agent-orchestrations/tool-options`; conditional router helper used again in Task 7.

- [ ] **Step 1: Write failing real-PG API tests**

In the new test module override only the local `client` fixture so `dbSession` continues to use the same engine:

```python
import pytest
from app.config import getSettings
from app.tests import _pg_support


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        getSettings.cache_clear()


async def test_disabled_feature_returns_404(monkeypatch):
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
    getSettings.cache_clear()
    async for ac in _pg_support.pgApiClient():
        response = await ac.get("/api/v1/agent-orchestrations")
        assert response.status_code == 404
    getSettings.cache_clear()
```

Add full-API tests that create an ACTIVE Agent through `/api/v1/agents`, create a tool through the existing tool API with `handler_ref="wiki_search"`, then verify:

1. profile create returns 201, `enabled=false`, `revision=1` and ordered bindings;
2. get/list return the new profile without changing `/api/v1/agents/options`;
3. update with stale revision returns 409;
4. toggle explicitly enables and disables;
5. owner can write, unrelated non-admin gets 403;
6. disabled tool, unknown handler, invalid schema and invalid system-ceiling budget return 422;
7. `GET /agent-orchestrations/tool-options` returns only currently enabled, valid, whitelisted tools with `toolName`, `description`, `inputSchema`, and no `handlerRef`;
8. default-disabled `client` in another test module still has no new route.

Use existing test helpers for Agent/tool payloads; if their field names differ, copy the exact payload from `test_agent_runtime_api.py` rather than guessing.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_orchestration_config_api.py -q
```

Expected: 404 for enabled cases because routers/services do not exist.

- [ ] **Step 3: Implement configuration service with audit and revision checks**

Use `AgentRegistryService.getAgent` to resolve the Agent, `AclService.assertCanModify(user, agent.owner, "Agent", agent_code)` on every write, and a boolean equivalent for list filtering (`admin` or non-empty owner in departments). Create defaults to `enabled=False` regardless of request input; no create DTO contains `enabled`.

For create/update, deduplicate `tool_names` while preserving order, load each current `AgentToolConfig`, require enabled + `handler_kind="BUILTIN"` + whitelist + valid schema, and construct new binding objects. Full update requires matching `revision`, replaces bindings immutably as a new list, increments revision, and emits outbox `before`/`after` without secrets. Toggle also requires revision and increments it. Translate unique races to `ConflictError`; do not silently overwrite an existing profile.

Implement `listToolOptions(session)` with this exact return element shape:

```python
{
    "toolName": row.name,
    "description": row.description,
    "inputSchema": dict(row.input_schema),
}
```

Select only enabled BUILTIN rows whose `handler_ref` is in `READ_ONLY_HANDLER_REFS`, validate every schema, omit invalid rows with a warning containing only the tool name, and sort by `name`. This is a new endpoint; do not add these fields to `/agents/options`.

- [ ] **Step 4: Implement API and conditional registration**

Endpoints and status codes:

```text
POST   /agent-orchestrations                  201
GET    /agent-orchestrations                  200
GET    /agent-orchestrations/tool-options     200
GET    /agent-orchestrations/{agentCode}      200
PUT    /agent-orchestrations/{agentCode}      200
POST   /agent-orchestrations/{agentCode}/toggle 200
```

Declare `/tool-options` before `/{agentCode}` so FastAPI does not capture the static segment as an Agent code. Every route depends on `getCurrentUser`; router inclusion in both app factories is under the same block and dependencies:

```python
if settings.enableAgentOrchestration:
    from app.api.v1 import agent_orchestrations
    app.include_router(
        agent_orchestrations.router,
        prefix="/api/v1/agent-orchestrations",
        tags=["agent-orchestrations"],
        dependencies=[Depends(mustChangePasswordGuard), Depends(rateLimitDep)],
    )
```

Use the same logic with `testApp` in `_testapp.py`. Do not import or mount these routers outside the conditional.

- [ ] **Step 5: Run GREEN and non-regression**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_orchestration_config_api.py -q
uv run pytest app/tests/integration/test_agent_runtime_api.py app/tests/integration/test_agent_options_api.py -q
uv run ruff check app/services/agent_orchestration_config_service.py app/api/v1/agent_orchestrations.py app/main.py app/tests/_testapp.py
```

Expected: PASS; `/agents/options` response is unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/agent_orchestration_config_service.py backend/app/api/v1/agent_orchestrations.py backend/app/main.py backend/app/tests/_testapp.py backend/app/tests/integration/test_agent_orchestration_config_api.py
git commit -m "feat: add feature-flagged orchestration configuration API"
```

---

### Task 4: ACL Delegate, Current-Binding Tool Executor and Delete Guard

**Files:**
- Modify: `backend/app/services/agent_runtime_service.py`
- Create: `backend/app/services/agent_orchestration_tool_executor.py`
- Modify: `backend/app/services/agent_tool_config_service.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_tool_executor.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_tool_delete.py`
- Test: `backend/app/tests/integration/test_agent_runtime_api.py`

**Interfaces:**
- Produces: `AgentRuntimeService.enforcePolicies(entity,tool)`, `AgentOrchestrationToolExecutor(executionGuard: ExecutionGuard, toolRegistry: AgentToolConfigRegistry, runtime: AgentRuntimeService)`, `.prepare(session,run,toolName,args,leaseOwner,now) -> PreparedTool`, `.invoke(session,prepared,context) -> ToolResult`.

- [ ] **Step 1: Write failing delegation, zero-call and deletion tests**

For the delegate, build an Agent with layered policies and a fake `AgentTool`; verify deny-by-default, `FORBIDDEN` over wildcard `READ`, wildcard layers and lowercase historical layer names. Assert `service.enforcePolicies(entity,tool)` produces exactly the same outcome as `service._enforcePolicies(entity,tool)` for each case.

Executor unit tests use an in-memory service/repository fake, not SQLite, and assert `handler.await_count == 0` for unknown tool, unbound tool, disabled binding/tool, removed whitelist ref, schema mismatch and ACL denial. Also assert ACL denial raises `OrchestrationPauseError("PERMISSION_DENIED", ...)`, not a Planner correction.

Integration deletion tests use real PG:

```python
async def test_orchestration_binding_blocks_tool_delete(client, dbSession):
    # Arrange via public Agent/tool/orchestration APIs; then enable nothing.
    response = await client.delete("/api/v1/admin/tools/wiki_search")
    assert response.status_code == 409
    assert "orchestration" in response.json()["detail"].lower()
    remaining = (
        await dbSession.execute(
            select(AgentToolConfig).where(AgentToolConfig.name == "wiki_search")
        )
    ).scalar_one()
    assert remaining.name == "wiki_search"
```

Add a control using a newly created unbound tool and assert its deletion status/body matches the pre-change baseline.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_tool_executor.py -q
uv run pytest app/tests/integration/test_agent_orchestration_tool_delete.py -q
```

Expected: missing public delegate/executor and deletion currently succeeds despite the new binding.

- [ ] **Step 3: Add only the pure ACL delegate**

```python
def enforcePolicies(self, entity: AgentDefinition, tool: AgentTool) -> None:
    """Public delegate for separate runtimes; policy semantics remain here."""
    self._enforcePolicies(entity, tool)
```

Do not alter `run`, `_enforcePolicies`, L4 functions or their imports beyond required type checking.

- [ ] **Step 4: Implement two-phase prepare/invoke**

`prepare` receives `(session, run, toolName, args, leaseOwner, now)`. In this exact order it must:

1. call the store's `assertExecutionAllowed` for cancel, lease and budgets;
2. load current Agent/profile/binding/tool config and require ACTIVE + enabled states;
3. require the binding in both current DB and the run snapshot;
4. require current BUILTIN handler ref in `READ_ONLY_HANDLER_REFS`;
5. validate args against the current schema and reject unknown keys;
6. reload/resolve the current `AgentTool` and call `AgentRuntimeService.enforcePolicies`.

Return `PreparedTool(tool=tool,args=dict(validatedArgs))`; do not call the handler during prepare. `invoke` is only:

```python
return await prepared.tool.handler(session, dict(prepared.args), context)
```

The runner must create the TOOL step only after `prepare` succeeds. Convert current-state loss to `AGENT_UNAVAILABLE`, ACL errors to `PERMISSION_DENIED`, and schema/unknown-tool Planner mistakes to `PlannerProtocolError`; never include ACL policy details in Planner feedback.

- [ ] **Step 5: Add the delete guard without changing unbound deletion**

Before deleting `AgentToolConfig`, query bindings joined to profiles for `tool_name=name`. If present, raise `ConflictError` with stable code/detail `agent_tool_in_use_orchestration` and sorted Agent codes. Preserve the existing `_ToolInUseConflict` message and `referencingAgents` behavior when there is no orchestration binding. If both old and new references exist, include both lists without exposing tool configuration payloads.

- [ ] **Step 6: Run GREEN and legacy policy suites**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_tool_executor.py app/tests/unit/test_agent_runtime_service.py -q
uv run pytest app/tests/integration/test_agent_orchestration_tool_delete.py app/tests/integration/test_agent_runtime_api.py -q
```

Expected: PASS; all old runtime status codes/messages remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/agent_runtime_service.py backend/app/services/agent_orchestration_tool_executor.py backend/app/services/agent_tool_config_service.py backend/app/tests/unit/test_agent_orchestration_tool_executor.py backend/app/tests/integration/test_agent_orchestration_tool_delete.py backend/app/tests/integration/test_agent_runtime_api.py
git commit -m "feat: enforce orchestration tool ACL and read-only bindings"
```

---

### Task 5: Single-Action Planner and Untrusted Prompt Construction

**Files:**
- Create: `backend/app/services/agent_orchestration_planner.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_planner.py`

**Interfaces:**
- Consumes: Shared `MeteredCompletion` and `PlanningLlmClient` from Task 2; Task 6 supplies the real metered implementation.
- Produces: `parsePlannerAction(content) -> PlannerAction`, `AgentOrchestrationPlanner.plan(run,steps,client: PlanningLlmClient,correction=None) -> PlannerAction`.

- [ ] **Step 1: Write failing parser tests for every action and hostile output**

```python
import pytest
from app.services.agent_orchestration_planner import parsePlannerAction
from app.services.agent_orchestration_types import (
    AskUserAction, CallToolAction, FinishAction, PlannerProtocolError,
)


def test_parse_three_actions_and_fence():
    call = parsePlannerAction('```json\n{"action":"CALL_TOOL","toolName":"wiki_search","args":{"query":"risk"},"summary":"lookup"}\n```')
    assert call == CallToolAction("wiki_search", {"query": "risk"}, "lookup")
    assert parsePlannerAction('{"action":"ASK_USER","question":"which year?","reason":"missing_time"}') == AskUserAction("which year?", "missing_time")
    assert parsePlannerAction('{"action":"FINISH","answer":"done"}') == FinishAction("done")


@pytest.mark.parametrize("content", [
    "not-json", "[]", '{"action":"EXECUTE_SQL","sql":"select 1"}',
    '{"action":"CALL_TOOL","toolName":"wiki_search","args":[],"summary":"x"}',
    '{"action":"CALL_TOOL","toolName":"wiki_search","args":{},"summary":"x","handlerRef":"x"}',
    '{"action":"FINISH","answer":"a"}{"action":"FINISH","answer":"b"}',
])
def test_invalid_protocol_is_rejected(content):
    with pytest.raises(PlannerProtocolError):
        parsePlannerAction(content)
```

Add prompt tests using a fake metered client returning `MeteredCompletion`: assert the system prompt starts with `renderSecurityPreamble()`, question is wrapped as `USER_QUESTION`, observations as `AGENT_TOOL_RESULT`, injected text is escaped, and the catalog contains only name/description/schema—not `handler_ref`, endpoint or dispatcher.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_planner.py -q
```

Expected: new module import failure.

- [ ] **Step 3: Implement strict parser and bounded prompt builder**

Use `stripJsonFence` then `json.loads`; require one JSON object and exact per-action keys (`CALL_TOOL`: action/toolName/args/summary; `ASK_USER`: action/question/reason; `FINISH`: action/answer). Reject unknown keys, empty strings, non-object args and oversized action fields (summary 1000, question 4000, reason 200, answer 30000). Construct fresh args via `dict`.

`plan` takes `run`, persisted `steps`, a client exposing `async completeForPlanning(messages) -> MeteredCompletion`, and optional correction. Build messages from:

1. security preamble + instructions that exactly one JSON action is allowed, tools are sequential, observations are data, and answers must cite actual observations;
2. wrapped `instructions_snapshot` and original/supplemented `question`;
3. the run's tool snapshot reduced to `toolName`, `description`, `inputSchema`, `sortOrder`;
4. persisted PLAN summaries and TOOL observations in step order, never hidden chain-of-thought;
5. one bounded correction message for invalid protocol only.

Call `completeForPlanning`; on parser failure call `client.markOutputInvalid(usageId)` before raising. This preserves usage with `call_status=OUTPUT_INVALID`.

- [ ] **Step 4: Run GREEN and coverage**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_planner.py --cov=app.services.agent_orchestration_planner --cov-report=term-missing -q
uv run ruff check app/services/agent_orchestration_planner.py
```

Expected: PASS and module coverage ≥80%.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_orchestration_planner.py backend/app/tests/unit/test_agent_orchestration_planner.py
git commit -m "feat: add safe single-action orchestration planner"
```

---

### Task 6: Per-Call Metering, Failure Accounting and Cost Aggregation

**Files:**
- Create: `backend/app/services/agent_orchestration_metering.py`
- Modify: `backend/app/services/chat_usage.py` (public pure `UsageMixin.calculateCost` delegate only)
- Test: `backend/app/tests/unit/test_agent_orchestration_metering.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_metering.py`

**Interfaces:**
- Produces: `MeteredLlmClient.complete`, `completeForPlanning`, `complete_with_tools`, `completeStream`, `markOutputInvalid`; `MeteredLlmFactory`; `summarizeCostSources(sources) -> tuple[str,bool]`.

- [ ] **Step 1: Write failing cost and failure-path tests**

Unit tests:

```python
from decimal import Decimal
from app.services.agent_orchestration_metering import summarizeCostSources


def test_cost_source_and_completeness_rules():
    assert summarizeCostSources([]) == ("UNKNOWN", False)
    assert summarizeCostSources(["MEASURED", "MEASURED"]) == ("MEASURED", True)
    assert summarizeCostSources(["ESTIMATED", "ESTIMATED"]) == ("ESTIMATED", True)
    assert summarizeCostSources(["MEASURED", "ESTIMATED"]) == ("MIXED", True)
    assert summarizeCostSources(["MEASURED", "UNKNOWN"]) == ("UNKNOWN", False)
```

Use a fake `BaseLlmClient` to test successful `complete`, fenced-but-invalid Planner output, transport failure without usage, and failure with `LlmClientError.tokens`. Assert no fallback model is requested and `createClient` receives the same config object every time.

Integration tests create a run/step through real PG services, invoke the metered wrapper with a fake client, then query `agent_orchestration_usage`: exactly one row per response/failure, correct run/step/caller/model, cached tokens, cost source and immediate run/step sums. A parse failure must update the same usage row to `OUTPUT_INVALID`, not insert a second row.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_metering.py -q
uv run pytest app/tests/integration/test_agent_orchestration_metering.py -q
```

Expected: module does not exist.

- [ ] **Step 3: Expose the established cost math and implement usage recorder**

In `UsageMixin` (`chat_usage.py`, not a nonexistent `ChatUsageService`) add this public delegate and leave `_costFor` plus every existing caller unchanged:

```python
@staticmethod
def calculateCost(
    config: Any,
    promptTokens: int,
    completionTokens: int,
    cachedTokens: int | None = None,
    cacheHitMultiplier: float = 0.0,
) -> Decimal:
    return UsageMixin._costFor(
        config, promptTokens, completionTokens,
        cachedTokens=cachedTokens,
        cacheHitMultiplier=cacheHitMultiplier,
    )
```

New metering calls `UsageMixin.calculateCost` with the run's `LlmConfig`; before each recording it reads `LLM_CACHE_HIT_MULTIPLIER` through the existing `app.services.nl2sql_service._readFloatConfig(session,"LLM_CACHE_HIT_MULTIPLIER",0.0)` so cached-token pricing has the same semantics as Chat. Successful provider token usage priced locally is `ESTIMATED`, not `MEASURED`. `MEASURED` is accepted only by the low-level recorder for a future explicit provider-cost input; this phase must not invent provider cost. Ignore legacy `ToolResult.tokens_used/cost` fields in the new aggregate—the metered client rows are the fact source and must not be double counted.

Implement a recorder using its own short session per call:

```python
async def recordUsage(
    self, *, runId: int, stepId: int | None, callerType: str,
    modelConfigId: int, modelName: str, promptTokens: int,
    completionTokens: int, cachedTokens: int | None, cost: Decimal,
    costSource: str, callStatus: str,
) -> int:
```

Insert usage, flush for id, lock the run row with `SELECT ... FOR UPDATE`, atomically increment run and optional step token/cost counters, recompute run source from all usage rows with `summarizeCostSources`, commit and return id. The run lock prevents concurrent tool/Planner recorders from overwriting `cost_source` with a stale partial aggregate. Do not hold this transaction across an LLM call. On recorder failure, log run/step/caller context and raise; never silently drop the accounting failure.

- [ ] **Step 4: Implement the wrapper and fixed-model factory**

`MeteredLlmClient` wraps the existing client and implements the complete `BaseLlmClient` surface. `complete` records `SUCCEEDED` then returns the original `LlmResponse`; `completeForPlanning` returns `MeteredCompletion`. `markOutputInvalid(usageId)` updates only `call_status`, not token counts. `complete_with_tools` maps `LlmResponseWithTools.usage` to the same recorder. `completeStream` yields chunks unchanged and records the terminal usage, or `UNKNOWN` if the stream fails before terminal usage. `close` delegates without writing usage.

Catch `LlmClientError`: use carried tokens when present, otherwise zeros + `UNKNOWN`; record `FAILED`, then re-raise the original exception. Other exceptions record `UNKNOWN` and re-raise. No exception is swallowed and no raw provider detail is copied to run error fields.

`MeteredLlmFactory(sessionFactory,runId,stepId,callerType,config)` returns a callable accepting any ignored argument but always wrapping `createClient(self._config)`. If factory returns `None`, raise `LLMUnavailableError` and record an `UNKNOWN/FAILED` call. Construct a new factory per Planner/TOOL step so usage attaches to the correct step without mutable shared step state.

- [ ] **Step 5: Run GREEN and old cost tests**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_metering.py app/tests/integration/test_agent_orchestration_metering.py -q
uv run pytest app/tests/unit/test_chat_usage.py -q
uv run pytest app/tests/unit/test_agent_orchestration_planner.py -q
uv run ruff check app/services/agent_orchestration_metering.py app/services/chat_usage.py
```

Expected: PASS. If the old cost test has another filename, locate it with `rg -l "_costFor" app/tests` and run that exact file.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/agent_orchestration_metering.py backend/app/services/chat_usage.py backend/app/tests/unit/test_agent_orchestration_metering.py backend/app/tests/integration/test_agent_orchestration_metering.py
git commit -m "feat: meter every orchestration LLM call"
```

---

### Task 7: Run Service, Ownership and Run/Step/Resume/Cancel APIs

**Files:**
- Create: `backend/app/services/agent_orchestration_run_service.py`
- Create: `backend/app/api/v1/agent_runs.py`
- Modify: `backend/app/main.py`, `backend/app/tests/_testapp.py`
- Test: `backend/app/tests/integration/test_agent_runs_api.py`

**Interfaces:**
- Produces: `createRun`, `listRuns`, `getRun`, `listSteps`, `resumeRun`, `cancelRun`; run mappers used by frontend and Worker integration tests.

- [ ] **Step 1: Write failing real-PG API tests**

Using the enabled fixture pattern from Task 3 and public configuration APIs, verify:

- create requires `modelConfigId`, uses an active existing model, snapshots instructions/bindings/schema/budgets, returns 202 `QUEUED`;
- missing/disabled profile, non-ACTIVE Agent, no valid binding and limits over system ceilings return 409 or 422 as specified and create zero rows;
- list filters by `agentCode/status`, bounded `limit/offset`, and returns only the caller's runs;
- non-creator non-admin get/steps/resume/cancel returns uniform 404; admin can read;
- steps return ascending `stepNo` and honor `afterStepNo`;
- non-PAUSED resume returns 409; `NEEDS_INPUT/AMBIGUOUS` require input; `BUDGET_EXHAUSTED` requires the matching increment; successful resume returns 202 and clears pause fields;
- QUEUED/PAUSED cancel becomes `CANCELLED`; RUNNING cancel returns `RUNNING` with `cancelRequested=true`; terminal cancel returns 409;
- model id is unchanged after resume.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_runs_api.py -q
```

Expected: routes are absent.

- [ ] **Step 3: Implement creation, snapshots and read ACL**

Create UUID with `str(uuid.uuid4())`. Load Agent/profile/bindings/current tools/model in short transactions. Require `AgentStatus.ACTIVE`, enabled profile and at least one currently valid binding. Snapshot only valid tools in `sort_order` order as new dicts containing `toolName`, `description`, `inputSchema`, `sortOrder`; never include handler refs or credentials.

Merge provided limits over profile defaults and validate each against `Settings.agentOrchestrationMax*`. Initialize all counters to zero, `cost_source=UNKNOWN`, `cost_complete=false`, `queued_at=now`, `status=QUEUED`. Commit and audit `agent_orchestration_run` CREATE without recording model keys or tool config internals.

Read ACL is `run.created_by == user.userId or "admin" in user.roles`; all failures for another user's run raise the same `NotFoundError("agent run not found")`. Step pagination caps limit at 200 and orders by `step_no`.

- [ ] **Step 4: Implement explicit resume/cancel transitions**

Resume only from PAUSED. Enforce pause-specific input/budget rules, recheck current Agent/profile/model/bindings and, for `PERMISSION_DENIED`, run current ACL against each snapshotted tool before queueing. Add increments with `min(current+additional, systemCeiling)` only if the sum is within the ceiling; otherwise 422, never silently clamp. Append supplementary input to `question` with a labeled separator, clear pause/error/cancel fields, clear lease, set `started_at=None`, `status=QUEUED`, increment revision and audit.

Cancel QUEUED/PAUSED immediately to CANCELLED with `finished_at`; RUNNING only sets `cancel_requested=true`; terminal states raise 409. Return the actual current summary.

- [ ] **Step 5: Mount run router inside the same feature flag**

Add `agent_runs.router` at `/api/v1/agent-runs` in the exact conditional block from Task 3, with `mustChangePasswordGuard` and `rateLimitDep`. Apply `@limiter.limit(rateLimitValue)` to create/resume/cancel as on the old run endpoint; include a `Request` parameter so SlowAPI can identify the request. Read routes require `getCurrentUser` and do not allow anonymous execution.

- [ ] **Step 6: Run GREEN and flag/ownership checks**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_runs_api.py app/tests/integration/test_agent_orchestration_config_api.py -q
uv run pytest app/tests/integration/test_agent_runtime_api.py -q
uv run ruff check app/services/agent_orchestration_run_service.py app/api/v1/agent_runs.py
```

Expected: PASS, including disabled-feature 404 and unchanged old run behavior.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/agent_orchestration_run_service.py backend/app/api/v1/agent_runs.py backend/app/main.py backend/app/tests/_testapp.py backend/app/tests/integration/test_agent_runs_api.py
git commit -m "feat: add owned orchestration run lifecycle API"
```

---

### Task 8: Atomic Worker Claim, Lease and Graceful Polling

**Files:**
- Create: `backend/app/services/agent_orchestration_store.py`
- Create: `backend/app/workers/agent_orchestration_worker.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_worker_claim.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_worker.py`

**Interfaces:**
- Produces: `AgentOrchestrationStore.claimNext`, `assertExecutionAllowed`, `renewLease`; `AgentOrchestrationWorker(settings: Settings, sessionFactory: async_sessionmaker[AsyncSession], runner: ClaimedRunRunner).runOnce/run/requestStop`.

- [ ] **Step 1: Write failing two-worker and feature-flag tests**

Real-PG integration test: create two QUEUED runs, call `claimNext` concurrently from independent sessions/workers 100 times, collect successfully claimed run ids, and assert each run has exactly one owner. Assert a live RUNNING lease is not reclaimable, an expired one is, and a stale owner cannot renew.

Unit tests use a fake store/runner: disabled Worker does not poll or run; enabled `runOnce` claims and awaits `runner.runClaimed`; runner exception is logged and polling continues; `requestStop` prevents another iteration. Do not assert sleep-based timing; invoke `runOnce` directly.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_orchestration_worker_claim.py app/tests/unit/test_agent_orchestration_worker.py -q
```

Expected: new classes are absent.

- [ ] **Step 3: Implement atomic claim and lease checks**

`claimNext(session,workerId,leaseSeconds,now)` first performs a short conditional recovery update for one expired RUNNING row: set `status=QUEUED`, clear `lease_owner/lease_until`, **clear `started_at`**, and increment revision only where the same expired lease still matches. It then selects a QUEUED candidate ordered by id with `FOR UPDATE SKIP LOCKED` and conditionally updates it to RUNNING with owner/lease/revision. Set `started_at=now` on every QUEUED → RUNNING claim, including recovery and resume. This makes the active timeout window restart when a new owner takes over; paused/queued time and an expired dead Worker's window never count against the new owner. Commit the claim before returning. Return `None` when no row is available. These two short transactions make the externally persisted recovery transition `RUNNING → QUEUED → RUNNING`; neither transaction spans an LLM/tool call.

`assertExecutionAllowed` reloads with `FOR UPDATE`, checks `cancel_requested` first, exact `lease_owner`, unexpired lease, `RUNNING` status, then iteration/tool/cost/time budgets. Raise `OrchestrationPauseError` with `BUDGET_EXHAUSTED` and detail `{budget:"iteration|tool|cost|time"}`; lost lease/cancel uses a dedicated control-flow exception, not a Planner correction. `renewLease` is a conditional UPDATE with owner + RUNNING + current lease and returns false for stale owners.

- [ ] **Step 4: Implement opt-in Worker lifecycle**

Worker constructor accepts settings, session factory and the `ClaimedRunRunner` protocol; there is no module-level import of the later Runner implementation. `run` returns immediately with an info log when the feature flag is false. Otherwise register SIGTERM/SIGINT, poll `runOnce`, and wait on `asyncio.Event` for `agentOrchestrationPollSeconds`. Each run uses a new session; no LLM/tool call occurs inside the claim transaction. Unexpected errors are logged with run/worker ids and the loop continues.

`main()` constructs the default dependencies and uses a function-local `from app.services.agent_orchestration_runner import AgentOrchestrationRunner` import; Task 9 supplies that implementation and its real-Worker acceptance test. The following CLI is the stable entry point after Task 9, not a Task 8 test command:

```bash
cd backend
ENABLE_AGENT_ORCHESTRATION=true uv run python -m app.workers.agent_orchestration_worker
```

Do not start this process as part of normal API startup.

- [ ] **Step 5: Run GREEN and race repetition**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_orchestration_worker_claim.py app/tests/unit/test_agent_orchestration_worker.py -q
uv run pytest app/tests/integration/test_agent_orchestration_worker_claim.py --count=1 -q
```

The repository may not have `pytest-repeat`; if `--count` is unrecognized, rerun the same plain pytest command instead and record that the second invocation passed. Do not install a new plugin just for repetition.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/agent_orchestration_store.py backend/app/workers/agent_orchestration_worker.py backend/app/tests/integration/test_agent_orchestration_worker_claim.py backend/app/tests/unit/test_agent_orchestration_worker.py
git commit -m "feat: add leased orchestration worker claims"
```

---

### Task 9: Bounded Runner, Pause Semantics and Crash-Safe Step Replay

**Files:**
- Modify: `backend/app/services/agent_orchestration_store.py`
- Create: `backend/app/services/agent_orchestration_runner.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_runner.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_runner_recovery.py`

**Interfaces:**
- Consumes: Planner, executor, metering and Worker claim from Tasks 4–8.
- Produces: `AgentOrchestrationRunner.runClaimed(runId,workerId)`; store methods `getOrCreatePlanStep`, `getOrCreateToolStep`, `completePlanStep`, `completeToolStep`, `pauseRun`, `succeedRun`, `cancelRun`.

- [ ] **Step 1: Write failing state-machine and budget tests**

Use fake Planner/executor/metering with real store fakes for unit tests. Cover:

1. `FINISH` writes answer on PLAN + run and succeeds;
2. `ASK_USER` pauses `NEEDS_INPUT`, or `AMBIGUOUS` when reason contains `ambiguous`;
3. two sequential `CALL_TOOL` actions produce PLAN1/TOOL1/PLAN2/TOOL2/PLAN3 in order;
4. unknown action/schema feedback is retried at most `agentOrchestrationMaxPlannerCorrections`, then `PLANNER_OUTPUT_INVALID`;
5. permission denial pauses immediately and sends no ACL detail back to Planner;
6. iteration/tool/cost/time budgets prevent the next LLM/handler call; a legal FINISH that crosses cost still succeeds after metering;
7. repeated tool exceptions pause `TOOL_FAILED` after two consecutive failures; successful tool resets the consecutive count;
8. infrastructure errors pause `INFRASTRUCTURE_ERROR` after bounded retries, permanent invariant violations become `FAILED` with sanitized error code/message;
9. RUNNING cancel is observed at the next safety point, after the current read-only tool finishes;
10. no new call occurs after stale lease or cancellation.

- [ ] **Step 2: Write failing real-PG crash-replay test**

Create a run and lease, let the fake handler return successfully, then make `completeToolStep` raise a test-only `SimulatedWorkerCrash(BaseException)` before commit. Expire the lease with SQL and reclaim. Assert:

- the same TOOL `step_no` and original `input_json` are reused;
- handler is invoked again with identical args;
- only one TOOL row exists for that action;
- original PLAN and all prior usage remain;
- final output commits before the next PLAN step is created.

Also test crash while PLAN is RUNNING: replanning reuses the PLAN row and does not increment `iteration_count` twice. Separately test crash after a PLAN row was committed as `SUCCEEDED/CALL_TOOL` but before TOOL creation: the recovered Worker must reconstruct that exact persisted action and create its TOOL step without another Planner LLM call. A committed FINISH or ASK_USER PLAN is likewise transitioned to SUCCEEDED/PAUSED without replanning.

- [ ] **Step 3: Run RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_runner.py app/tests/integration/test_agent_orchestration_runner_recovery.py -q
```

Expected: runner/store transition methods are missing.

- [ ] **Step 4: Implement lease-checked persistence transitions**

Each store method uses a short transaction and conditional run update keyed by `run_id + lease_owner + RUNNING`. `getOrCreatePlanStep` first returns an existing RUNNING PLAN for the current iteration; otherwise allocates `max(step_no)+1`, inserts RUNNING PLAN and increments `iteration_count` once in the same transaction. `getOrCreateToolStep` uses the `(run_id, iteration_no, kind=TOOL)` recovery lookup, reuses original `input_json`, and increments `tool_call_count` only on first insert.

`completeToolStep` updates the original row to SUCCEEDED/FAILED with bounded observation/error and commits before returning. Every completion verifies the current lease; stale owners cannot update steps or run. `pauseRun`, `succeedRun`, `cancelRun` clear lease and set `finished_at` only for terminal states. No hidden reasoning text is persisted—only the explicit short `plan_summary`.

- [ ] **Step 5: Implement the runner loop**

For each active iteration:

1. assert lease/cancel/budgets and renew lease;
2. inspect the latest persisted step before planning: a SUCCEEDED PLAN with CALL_TOOL and no TOOL supplies the original action; a SUCCEEDED FINISH/ASK_USER PLAN completes its original transition; only a missing or RUNNING PLAN invokes the Planner;
3. get/reuse PLAN step and, only when replanning is required, construct its fixed-model metered client;
4. load persisted steps, call Planner when required, and record the valid action on PLAN;
5. process FINISH before any post-response budget check so a legal over-budget answer can succeed; process ASK_USER as its specified pause;
6. for CALL_TOOL, check tool budget, then call `prepare`;
7. only after prepare succeeds, get/reuse TOOL step, construct its metered `AgentToolContext(llm_factory=...,llm_config=exact config,actor=run.created_by)`;
8. invoke outside DB transactions; serialize observation and complete TOOL before the next iteration;
9. at the next safety point check cancel/lease/budgets before any new LLM or tool call.

Protocol errors become bounded correction input on the same PLAN; they never create TOOL rows. Permission errors pause directly. Tool exceptions save a sanitized failed TOOL observation and may be reconsidered by Planner until the consecutive-failure limit. Never persist stack traces, connection strings, `.env`, provider headers or raw exception repr.

- [ ] **Step 6: Run GREEN, coverage and focused non-regression**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_runner.py app/tests/integration/test_agent_orchestration_runner_recovery.py -q
uv run pytest app/tests/unit/test_agent_orchestration_planner.py app/tests/unit/test_agent_orchestration_tool_executor.py app/tests/unit/test_agent_orchestration_metering.py -q
uv run pytest app/tests/unit/test_agent_orchestration_runner.py app/tests/unit/test_agent_orchestration_planner.py app/tests/unit/test_agent_orchestration_tool_executor.py app/tests/unit/test_agent_orchestration_metering.py --cov=app.services.agent_orchestration_runner --cov=app.services.agent_orchestration_store --cov=app.services.agent_orchestration_planner --cov=app.services.agent_orchestration_tool_executor --cov=app.services.agent_orchestration_metering --cov-report=term-missing -q
uv run ruff check app/services/agent_orchestration_runner.py app/services/agent_orchestration_store.py
```

Expected: PASS and combined new-module line coverage ≥80%; separately inspect uncovered branches and add behavior tests rather than excluding them.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/agent_orchestration_store.py backend/app/services/agent_orchestration_runner.py backend/app/tests/unit/test_agent_orchestration_runner.py backend/app/tests/integration/test_agent_orchestration_runner_recovery.py
git commit -m "feat: execute recoverable bounded orchestration runs"
```

---

### Task 10: End-to-End Real-PG Acceptance and Physical Non-Regression

**Files:**
- Test: `backend/app/tests/integration/test_agent_orchestration_acceptance.py`
- Test: `backend/app/tests/integration/test_agent_runtime_api.py`
- Test: existing Chat, scheduler and research suites only where a new assertion is needed

**Interfaces:**
- Consumes: all backend tasks.
- Produces: acceptance evidence for spec §10.2 and §10.3; no new runtime API.

- [ ] **Step 1: Write full API → Worker → steps acceptance tests**

Use real PostgreSQL, public API setup, an actual Worker/store/runner, and deterministic fake LLM responses at the `BaseLlmClient` boundary. Keep current binding/schema/ACL checks real. Cover the eight spec scenarios explicitly:

1. two different tools then FINISH; query steps through API and assert final answer plus usage;
2. ASK_USER → PAUSED → resume with input → QUEUED → SUCCEEDED;
3. ACL denial → PAUSED with zero handler calls; another user gets 404;
4. concurrent claim uniqueness (reuse the race test as a required suite, not a skipped reference);
5. post-handler/pre-commit crash replay (reuse Task 9 required suite);
6. budget and fixed-model usage facts, including tool-internal `complete`;
7. disabled feature 404 while old run endpoint remains available;
8. Agent hard delete leaves run with `agent_id=NULL` and historical `agent_code`; bound tool delete 409 and unbound tool delete baseline unchanged.

No test may call only a service and claim API coverage. Worker execution may be invoked directly after API creation because Worker is a separate process in production; all user-facing setup/assertions still go through HTTP and real PG.

- [ ] **Step 2: Add explicit physical-separation assertions**

Extend old runtime integration coverage: after creating an orchestration profile for the same Agent, `POST /api/v1/agents/{code}/run` still invokes only legacy `tool_name`, returns the old `AgentRunRead` shape, and `agent_orchestration_run` count remains zero. Preserve the no-legacy-tool 409 case even when a valid multi-tool profile exists.

For scheduler, spy on `AgentRuntimeService.run` and the new runner; a due schedule must call the former exactly once and the latter never. For Chat explicit Agent and L4, use existing test entry points and assert new run count remains zero/new runner spy is never called. Run the research API suite unchanged; do not add orchestration hooks to make a test more convenient.

- [ ] **Step 3: Run acceptance and all affected legacy suites**

```bash
cd backend
uv run pytest app/tests/integration/test_agent_orchestration_acceptance.py app/tests/integration/test_agent_orchestration_worker_claim.py app/tests/integration/test_agent_orchestration_runner_recovery.py -q
uv run pytest app/tests/integration/test_agent_runtime_api.py app/tests/integration/test_agent_scheduler_api.py app/tests/integration/test_research_api.py -q
uv run pytest app/tests/unit/test_chat_service.py app/tests/unit/test_agent_runtime_service.py -q
```

Expected: PASS. Resolve actual suite filenames with `rg -l` before execution; a missing/un collected path is not evidence of success.

- [ ] **Step 4: Review security and commit test-only changes**

Run `code-reviewer` and `security-reviewer` on the complete backend diff. Address CRITICAL/HIGH findings before commit; specifically inspect fail-open feature flags, stale lease writes, unknown JSON keys, ACL feedback, error sanitization and missing metering.

```bash
git add backend/app/tests/integration/test_agent_orchestration_acceptance.py backend/app/tests/integration/test_agent_runtime_api.py
git commit -m "test: prove orchestration acceptance and legacy isolation"
```

If legacy test files beyond those two were modified, add exactly those files to the commit as well.

---

### Task 11: Opt-In Compose Worker and Gray-Rollout Runbook

**Files:**
- Modify: `docker/docker-compose.yml`, `docker/docker-compose.centos.yml`
- Create: `backend/scripts/seed_agent_orchestration_menu.py`
- Test: `backend/app/tests/unit/test_agent_orchestration_menu_seed.py`
- Test: `backend/app/tests/integration/test_agent_orchestration_menu_seed.py`
- Create: `Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md`

**Interfaces:**
- Produces: `agent-orchestration-worker` Compose profile and explicit menu seed; default deployment remains unchanged.

- [ ] **Step 1: Write failing deployment-shape and menu tests**

Unit test parses both Compose YAML files and asserts one Worker service with `profiles: ["agent-orchestration"]`, the same backend image/build as the existing backend, `ENABLE_AGENT_ORCHESTRATION=true`, command `python -m app.workers.agent_orchestration_worker`, and no published ports. Assert the default backend service does not contain that command and no API service auto-starts the Worker.

Integration test seeds normal menu config and asserts the two new paths are absent; invokes the explicit new seed twice and asserts exactly one `item.agentOrchestration` and one `item.agentTasks`, both under `section.aiAgent`, without overwriting an existing UI-edited row.

- [ ] **Step 2: Run RED**

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_menu_seed.py app/tests/integration/test_agent_orchestration_menu_seed.py -q
```

Expected: Worker profile/menu seed are absent.

- [ ] **Step 3: Add Compose profile and explicit menu seed**

Keep Worker dependencies, env file, network, restart policy and image conventions identical to the existing scheduler/backend service in each file. Do not use Compose `profiles` on the API and do not add the Worker to any default `up` documentation command.

The new menu script uses `pg_insert(...).on_conflict_do_nothing(index_elements=["code"])`, resolves `section.aiAgent` by code, and inserts:

```python
{"code":"item.agentOrchestration","label_key":"menu.item.agentOrchestration","icon_code":"appstore","sort_order":140,"path":"/agent-orchestrations"}
{"code":"item.agentTasks","label_key":"menu.item.agentTasks","icon_code":"thunderbolt","sort_order":150,"path":"/agent-tasks"}
```

Do not add these items to the default `seed_menu_config.ITEMS`; this is what prevents premature exposure.

- [ ] **Step 4: Write the staged runbook and validate Compose**

Document only user-executed production steps: backup/migrate with flag off; deploy images; verify new API 404 and old contracts; enable in test with Worker; run acceptance; create one read-only profile; explicitly seed menu; browser smoke; enable production only after approval; stop Worker/flag off for rollback; retain new tables. Include lease-drain and `PAUSED/CANCELLED` checks. Never instruct regeneration of `SECRET_KEY` and never print `.env`.

Run:

```bash
cd backend
uv run pytest app/tests/unit/test_agent_orchestration_menu_seed.py app/tests/integration/test_agent_orchestration_menu_seed.py -q
docker compose -f docker/docker-compose.yml config --quiet
docker compose -f docker/docker-compose.yml --profile agent-orchestration config --quiet
docker compose -f docker/docker-compose.centos.yml config --quiet
```

Expected: PASS and valid Compose. Do not run `up` against production.

- [ ] **Step 5: Commit**

```bash
git add docker/docker-compose.yml docker/docker-compose.centos.yml backend/scripts/seed_agent_orchestration_menu.py backend/app/tests/unit/test_agent_orchestration_menu_seed.py backend/app/tests/integration/test_agent_orchestration_menu_seed.py Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md
git commit -m "feat: add opt-in orchestration worker rollout"
```

---

### Task 12: Frontend Contracts and API Client

**Files:**
- Create: `frontend/src/types/agentOrchestration.ts`
- Create: `frontend/src/api/agentOrchestrationApi.ts`
- Test: `frontend/src/tests/agentOrchestrationApi.test.ts`

**Interfaces:**
- Produces: typed functions consumed by both pages; no changes to `agentRegistry.ts`.

- [ ] **Step 1: Write failing API tests**

Mock `../api/client` and assert exact methods, paths, payloads and query params for all twelve functions:

```typescript
createOrchestration, listOrchestrations, listOrchestrationToolOptions,
getOrchestration, updateOrchestration, toggleOrchestration,
createAgentRun, listAgentRuns, getAgentRun, getAgentRunSteps,
resumeAgentRun, cancelAgentRun
```

Test all twelve and do not silently drop cancel. Include `getAgentRunSteps(id,{afterStepNo:40,limit:100})` expecting `"/agent-runs/id/steps"` with `{params:{afterStepNo:40,limit:100}}`, and list filters omitting undefined values.

- [ ] **Step 2: Run RED**

```bash
cd frontend
npm test -- --run src/tests/agentOrchestrationApi.test.ts
```

Expected: module import failure.

- [ ] **Step 3: Implement explicit types and HTTP functions**

Define string unions for run status, pause code, step kind/status, Planner action and cost source. Define interfaces matching Task 2 JSON exactly, including `costComplete`, `pauseDetail`, snapshots and `cancelRequested`. Use `JsonObject = {[key:string]:unknown}` for tool args/output; do not use `any`.

Use `httpClient` and two constants `ORCHESTRATION_PREFIX="/agent-orchestrations"`, `RUN_PREFIX="/agent-runs"`. Return `response.data`; URI-encode path parameters with `encodeURIComponent`. All exported functions have explicit parameter and return types.

- [ ] **Step 4: Run GREEN and typecheck**

```bash
cd frontend
npm test -- --run src/tests/agentOrchestrationApi.test.ts src/tests/agentRegistryApi.test.ts
npm run typecheck
```

If `typecheck` is absent, use the existing script found by `npm run` (typically `tsc -b --noEmit` through `build`); report the exact command rather than claiming an unavailable script passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/agentOrchestration.ts frontend/src/api/agentOrchestrationApi.ts frontend/src/tests/agentOrchestrationApi.test.ts
git commit -m "feat: add orchestration frontend API contracts"
```

---

### Task 13: Orchestration Profile Management Page

**Files:**
- Create: `frontend/src/pages/AgentOrchestrationPage.tsx`
- Test: `frontend/src/tests/AgentOrchestrationPage.test.tsx`
- Modify: `frontend/src/i18n/zh-CN.ts`, `frontend/src/i18n/en-US.ts`

**Interfaces:**
- Consumes: existing `listAgents`, Task 12 `listOrchestrationToolOptions`, and Task 12 configuration API.
- Produces: independently testable profile management UI; does not modify old Agent Registry.

- [ ] **Step 1: Write failing page behavior tests**

Render with `MemoryRouter`, `ConfigProvider` and mocked APIs. Cover:

1. loads Agents and whitelisted tool options, creates profile with selected tools and numeric defaults;
2. new profile visibly shows disabled until explicit toggle;
3. editing sends `revision`, instructions, ordered `toolNames` and all defaults;
4. stale revision 409 shows a friendly conflict message;
5. tool options render only the additive endpoint's `toolName` values and never expect `handlerRef` from legacy `/agents/options`;
6. failed loads and rejected saves surface errors, with no unhandled `validateFields` rejection;
7. old `runAgent` mock is never called.

- [ ] **Step 2: Run RED**

```bash
cd frontend
npm test -- --run src/tests/AgentOrchestrationPage.test.tsx
```

Expected: page module is absent.

- [ ] **Step 3: Implement the focused management page**

Use Ant Design `Form`, `Select`, `InputNumber`, `Switch`, `Table` and `App.useApp()` message. Local state updates use new arrays/objects. On submit, `await form.validateFields()` inside `try/catch`; validation failure returns without an unhandled rejection. Preserve server `revision` in state and never derive `runnable` from profile state.

Split local presentational components into `frontend/src/components/agent/OrchestrationProfileForm.tsx` if the page would exceed 400 lines; give it explicit props and test through the page. Do not introduce global state or modify Chat/AgentRuntime stores.

Add i18n keys under a new `agentOrchestration` root and menu keys under both existing menu locations in each locale. Use interpolation `{count}` rather than hardcoding composed messages.

- [ ] **Step 4: Run GREEN and legacy page tests**

```bash
cd frontend
npm test -- --run src/tests/AgentOrchestrationPage.test.tsx src/tests/AgentRuntimePage.test.tsx src/tests/agentOrchestrationApi.test.ts
npm run typecheck
```

Expected: PASS and no console errors/unhandled rejections.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AgentOrchestrationPage.tsx frontend/src/tests/AgentOrchestrationPage.test.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts
```

If the form was split, add its exact frontend file and tests too. Then:

```bash
git commit -m "feat: add orchestration profile management page"
```

---

### Task 14: Task Creation, Polling, Resume/Cancel and Timeline

**Files:**
- Create: `frontend/src/pages/AgentTaskPage.tsx`
- Create: `frontend/src/components/agent/AgentRunTimeline.tsx`
- Test: `frontend/src/tests/AgentTaskPage.test.tsx`
- Test: `frontend/src/tests/AgentRunTimeline.test.tsx`

**Interfaces:**
- Consumes: Task 12 run APIs and existing `listModels(activeOnly: boolean): Promise<ModelConfig[]>` from `frontend/src/api/modelConfig.ts`; the page must call `listModels(true)`.
- Produces: complete user task flow with incremental steps and accessible status/error display.

- [ ] **Step 1: Write failing timeline and page tests**

Timeline tests assert PLAN summary, TOOL name/args/observation, truncation marker, failure message, final answer and cost-source badge (`UNKNOWN` visibly says incomplete). Assert raw hidden reasoning fields are not rendered or expected.

Page tests use fake timers only for poll advancement and real promises for API resolution:

1. select enabled profile + explicit active model, create run, navigate/select returned run;
2. poll only while QUEUED/RUNNING, request steps with the last `stepNo`, and append deduplicated sorted steps immutably;
3. stop polling on terminal/PAUSED and on unmount;
4. PAUSED `NEEDS_INPUT` requires supplementary input; budget pause exposes only relevant increments;
5. RUNNING cancel displays still-running cancel-requested state until a later poll reports CANCELLED;
6. 404 and network errors show friendly messages without fabricating a terminal run;
7. two sequential tool steps and final answer are visible;
8. no Chat API/store or legacy `runAgent` call occurs.

- [ ] **Step 2: Run RED**

```bash
cd frontend
npm test -- --run src/tests/AgentTaskPage.test.tsx src/tests/AgentRunTimeline.test.tsx
```

Expected: components are absent.

- [ ] **Step 3: Implement timeline and task page**

`AgentRunTimeline` receives `steps: readonly AgentRunStep[]`, `finalAnswer?: string|null`, `costSource`, `costComplete`; render JSON with `<pre>` and escaped React text, never `dangerouslySetInnerHTML`. Keep observation text collapsed by default and show truncation/error badges.

`AgentTaskPage` owns selected run, steps and polling. Use one effect keyed by `selectedRunId/status`, clear the timer on cleanup, and guard asynchronous responses with a cancelled flag. Maintain `lastStepNoRef`; merge by `stepNo` into a new sorted array. Do not reset steps on every poll. Poll interval is a named `RUN_POLL_MS=2000`; all API calls use per-call long timeout only if the existing client supports it—create/run/resume are short API operations and must not wait for Worker completion.

Resume/cancel errors are narrowed from `unknown`; use `AppError.status` rather than nonexistent Axios `.response`. Disable duplicate submissions while requests are in flight.

- [ ] **Step 4: Run GREEN, typecheck and old Chat tests**

```bash
cd frontend
npm test -- --run src/tests/AgentTaskPage.test.tsx src/tests/AgentRunTimeline.test.tsx src/tests/AgentRuntimePage.test.tsx src/tests/chatStore.test.ts
npm run typecheck
```

Expected: PASS. Use the actual Chat store test paths from `rg --files src | rg 'chatStore.*test'` if the listed file does not exist.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AgentTaskPage.tsx frontend/src/components/agent/AgentRunTimeline.tsx frontend/src/tests/AgentTaskPage.test.tsx frontend/src/tests/AgentRunTimeline.test.tsx
git commit -m "feat: add orchestration task tracking UI"
```

---

### Task 15: Additive Routes, Menu i18n and Browser-Level Smoke

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/i18n/zh-CN.ts`, `frontend/src/i18n/en-US.ts` only if keys remain
- Test: `frontend/src/tests/App.test.tsx`
- Test: `frontend/src/tests/menuI18n.test.ts`
- Create: `frontend/e2e/agent-orchestration.spec.ts` only if the repository already has Playwright configured; otherwise extend the existing E2E location/framework

**Interfaces:**
- Produces: `/agent-orchestrations` and `/agent-tasks`; no route changes under `/agents`, Chat or research.

- [ ] **Step 1: Write failing route and menu tests**

Extend App route tests to mock both new pages and assert direct navigation renders them while `/agents/run` still renders `AgentRuntimePage` and `/agents` still renders `AgentRegistryPage`. Extend menu i18n key coverage for `agentOrchestration` and `agentTasks`; verify default menu seed does not expose them until Task 11's explicit seed runs.

- [ ] **Step 2: Run RED**

```bash
cd frontend
npm test -- --run src/tests/App.test.tsx src/tests/menuI18n.test.ts
```

Expected: new routes/keys fail.

- [ ] **Step 3: Add only imports and two sibling routes**

```tsx
<Route path="agent-orchestrations" element={<AgentOrchestrationPage />} />
<Route path="agent-tasks" element={<AgentTaskPage />} />
```

Place them beside, not inside, existing Agent routes. Do not add redirects from old paths.

- [ ] **Step 4: Run GREEN and critical-flow E2E**

```bash
cd frontend
npm test -- --run src/tests/App.test.tsx src/tests/menuI18n.test.ts src/tests/AgentRuntimePage.test.tsx
npm run typecheck
```

Run the repository's existing Playwright command against a local stack with the feature explicitly enabled and a deterministic test LLM. Smoke flow: login through real auth fixture → create disabled profile → bind two tools → toggle → create task → pause on ASK_USER → resume → view two tool steps/final answer → cancel a second run. Do not fabricate auth tokens or bypass authentication. If no local E2E environment is available, report the browser flow as not executed; Vitest coverage does not substitute for a claim that E2E passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/App.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/tests/App.test.tsx frontend/src/tests/menuI18n.test.ts
git commit -m "feat: route independent orchestration pages"
```

---

### Task 16: Full Verification, Coverage, Documentation and Handoff

**Files:**
- Create: `Harness/changes/feat-agent-orchestration/summary.md`
- Modify: `Harness/wiki/agent-runtime.md`
- Modify: `Harness/agents/owner.md` (add the new change/Wiki links only)
- Create: `docs/superpowers/plans/2026-10-26-general-agent-orchestration-verification.md`

**Interfaces:**
- Produces: reproducible verification evidence and SSOT; no production changes.

- [ ] **Step 1: Run focused backend and frontend quality gates**

```bash
cd backend
uv run ruff check app/domain/agent_orchestration_models.py app/domain/agent_orchestration_schemas.py app/services/agent_orchestration_types.py app/services/agent_orchestration_validation.py app/services/agent_orchestration_config_service.py app/services/agent_orchestration_run_service.py app/services/agent_orchestration_planner.py app/services/agent_orchestration_metering.py app/services/agent_orchestration_tool_executor.py app/services/agent_orchestration_store.py app/services/agent_orchestration_runner.py app/workers/agent_orchestration_worker.py app/api/v1/agent_orchestrations.py app/api/v1/agent_runs.py
uv run pytest app/tests/unit/test_agent_orchestration_config.py app/tests/unit/test_agent_orchestration_schemas.py app/tests/unit/test_agent_orchestration_validation.py app/tests/unit/test_agent_orchestration_tool_executor.py app/tests/unit/test_agent_orchestration_planner.py app/tests/unit/test_agent_orchestration_metering.py app/tests/unit/test_agent_orchestration_worker.py app/tests/unit/test_agent_orchestration_runner.py --cov=app.services.agent_orchestration_types --cov=app.services.agent_orchestration_validation --cov=app.services.agent_orchestration_config_service --cov=app.services.agent_orchestration_run_service --cov=app.services.agent_orchestration_planner --cov=app.services.agent_orchestration_metering --cov=app.services.agent_orchestration_tool_executor --cov=app.services.agent_orchestration_store --cov=app.services.agent_orchestration_runner --cov=app.workers.agent_orchestration_worker --cov-report=term-missing -q
```

Require combined new-code coverage ≥80%; also run integration coverage for config/run/store paths before concluding. Check every new file with `wc -l`; split any file over 800 and any function over 50 lines by responsibility without changing behavior.

```bash
cd frontend
npm run typecheck
npm test -- --run src/tests/agentOrchestrationApi.test.ts src/tests/AgentOrchestrationPage.test.tsx src/tests/AgentTaskPage.test.tsx src/tests/AgentRunTimeline.test.tsx src/tests/App.test.tsx src/tests/AgentRuntimePage.test.tsx
```

- [ ] **Step 2: Prove zero net-new failures against the actual baseline**

Record the feature-branch revision and ensure a clean tree. Run the full backend suite and save only failing node ids plus summaries outside the repository. Run full frontend tests and coverage separately; remember that frontend coverage may not be produced on a red run.

Create a fresh baseline worktree at the exact pre-feature base (`4d4114bb` unless execution rebased onto a newer explicitly recorded base), reuse dependency installation without shadowing source paths, apply the documented Alembic-test-version reset, and rerun only the feature branch's failing node ids that exist on baseline. A failure is net-new only if it fails on the feature branch and passes on baseline under the same isolated services. Fix all net-new failures; do not relabel them as pre-existing without baseline evidence.

Required legacy evidence includes old Agent runtime, scheduler, Chat explicit/semantic/L4 and research tests. A command that collects zero tests is a failed verification, not a pass.

- [ ] **Step 3: Perform final code/security review and adversarial checks**

Run `code-reviewer` and `security-reviewer` over `git diff <recorded-base>...HEAD`. Verify with tests or direct inspection:

- feature-off has no routers/Worker/menu;
- old run with a multi-tool profile cannot enter new runner;
- deleted Agent preserves run history;
- no stale lease can commit;
- permission denial does not enter Planner observation;
- every success, parse failure, transport failure and tool-internal LLM call has usage;
- UNKNOWN cost cannot become complete after later estimated calls;
- no write tool, SQL tool, dynamic handler ref or URL is accepted;
- logs/steps/API contain no secrets or stack traces;
- no default Compose command starts the Worker.

Resolve CRITICAL/HIGH findings and rerun affected suites.

- [ ] **Step 4: Write truthful SSOT and verification report**

`summary.md` records scope, schema, APIs, state machine, security, tests, rollout and known limits (read-only, sequential, polling, no NL2SQL tool). `agent-runtime.md` adds a third independent execution path and links the spec/runbook without rewriting L4 as the generic Agent. The verification report records exact commands, revisions, pass/fail counts, baseline comparison, coverage, unexecuted E2E/production items and residual risks. Never mark production deployed or E2E passed unless it actually happened.

- [ ] **Step 5: Commit documentation and present handoff**

```bash
git add Harness/changes/feat-agent-orchestration/summary.md Harness/changes/feat-agent-orchestration/deploy-gray-rollout.md Harness/wiki/agent-runtime.md Harness/agents/owner.md docs/superpowers/plans/2026-10-26-general-agent-orchestration-verification.md
git commit -m "docs: record orchestration verification and rollout"
```

Final handoff includes commit range, tests actually run, coverage, net-new-failure evidence, disabled-by-default confirmation and the manual gray-rollout runbook. Stop before any production operation or before enabling the feature publicly.

---

## Plan Self-Review Results

### Spec coverage

- Non-regression and physical split: Tasks 3, 7, 10, 16.
- Five additive tables and snapshots: Tasks 1, 7.
- Read-only binding/schema/ACL: Tasks 2, 4, 9.
- Three Planner actions and untrusted observations: Tasks 2, 5, 9.
- Per-call usage, failures, fixed model and cost completeness: Tasks 6, 9, 10.
- Full configuration/run APIs and ownership: Tasks 3, 7.
- Pause/resume/cancel/budgets: Tasks 7, 9, 10.
- Atomic claim, lease and crash replay: Tasks 8, 9, 10.
- Feature-off Compose and staged menu/deployment: Tasks 1, 3, 11.
- Independent frontend and trajectory: Tasks 12–15.
- Real-PG acceptance, ≥80% new-code coverage and zero net-new failures: Tasks 9, 10, 16.

### Placeholder scan

No implementation step is deferred to a later unspecified design. Conditional instructions name the exact discovery command and required action. There are no `TBD`, `TODO`, `implement later`, or “similar to Task” implementation placeholders. Test doubles are deterministic boundaries for external LLM/tool side effects; PostgreSQL, migrations, API routing and Worker persistence remain real.

### Type consistency

`PlannerAction`, `PreparedTool`, `MeteredCompletion`, `RunStatus`, `AgentRunRead` and `AgentRunStepRead` use the same names across Planner, executor, runner, API and frontend. `run.id` is the internal FK; `run.run_id` is the public UUID. `cost_source/cost_complete` map to `costSource/costComplete`. Resume keeps cumulative usage and starts a fresh active timeout window. PLAN/TOOL replay reuses the original run/step identity and arguments.
