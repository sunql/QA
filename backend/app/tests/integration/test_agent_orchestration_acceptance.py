"""Spec 10.2 acceptance: complete HTTP API, real Worker and real PostgreSQL.

Only provider responses and tool side effects are deterministic test doubles.
Configuration, snapshots, ACL, claims, state transitions and usage metering all
use production implementations. Scenarios 4 and 5 additionally have mandatory
focused race/crash suites; those suites are **not** re-run here (they run in the
same command per the brief), but their presence is asserted in-tree by
`test_requiredRaceAndCrashSuitesExistInTree` so that deleting or renaming a
mandatory suite fails the tree instead of silently degrading scenarios 4 and 5.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.agent_orchestration_models import (
    AgentOrchestrationRun,
    AgentOrchestrationUsage,
)
from app.domain.enums import MatchRule, SourceSystem
from app.domain.models import EntityMapping
from app.infrastructure import database as dbModule
from app.infrastructure.llm.base_client import (
    BaseLlmClient,
    LlmMessage,
    LlmResponse,
)
from app.infrastructure.llm.embedding_provider_factory import resetEmbeddingClientCache
from app.infrastructure.security import crypto
from app.services import agent_orchestration_metering, agent_tools
from app.services.agent_orchestration_runner import AgentOrchestrationRunner
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_tool_types import ToolResult
from app.tests import _pg_support
from app.workers.agent_orchestration_worker import AgentOrchestrationWorker

ADMIN_HEADERS = {
    "X-User-Id": "acceptance-admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
OWNER_HEADERS = {
    "X-User-Id": "acceptance-owner",
    "X-User-Roles": "analyst",
    "X-User-Departments": "procurement",
}
OUTSIDER_HEADERS = {
    "X-User-Id": "acceptance-outsider",
    "X-User-Roles": "analyst",
    "X-User-Departments": "sales",
}
AGENT_CODE = "ORCH_ACCEPTANCE_AGENT"
RUNS_PATH = "/api/v1/agent-runs"
PROFILES_PATH = "/api/v1/agent-orchestrations"
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}
CALL_A = (
    '{"action":"CALL_TOOL","toolName":"acceptance_a",'
    '"args":{"query":"alpha"},"summary":"call tool A"}'
)
CALL_B = (
    '{"action":"CALL_TOOL","toolName":"acceptance_b",'
    '"args":{"query":"beta"},"summary":"call tool B"}'
)
CALL_RISK = (
    '{"action":"CALL_TOOL","toolName":"acceptance_risk",'
    '"args":{"query":"supplier risk"},"summary":"assess supplier risk"}'
)
ASK_USER = (
    '{"action":"ASK_USER","question":"Which supplier?",'
    '"reason":"supplier identifier is missing"}'
)
FINISH = '{"action":"FINISH","answer":"acceptance final answer"}'
# 脚本化 LLM 每次调用的固定用量（用于把 usage 行的 token 数锚定到「独立来源」，
# 而不是让成本等式自己验证自己）。
_LLM_PROMPT_TOKENS = 11
_LLM_COMPLETION_TOKENS = 7
# 成本核对容差：USD 金额列 Numeric(20,8)，1e-8 即一个存储精度量子。
_COST_TOLERANCE = Decimal("1e-8")
# 场景 4/5 的深度依赖两个「必跑套件」。它们与本文件在同一条命令里执行，
# 这里只断言依赖**存在于测试树**（不重跑）：套件被删除/改名/用例被删，
# 本断言必须红——否则「只跑验收文件」会静默地把场景 4/5 降级成报告里的一句话。
_REQUIRED_SUITES: dict[str, tuple[str, ...]] = {
    "app.tests.integration.test_agent_orchestration_worker_claim": (
        "test_concurrent_claims_never_double_claim",
        "test_expired_lease_is_recovered_into_a_fresh_window",
    ),
    "app.tests.integration.test_agent_orchestration_runner_recovery": (
        "test_crash_after_handler_before_output_commit_replays_the_handler",
        "test_crash_after_tool_row_creation_replays_the_same_step_and_args",
    ),
}


@pytest.fixture
def orchestrationEnabled(request: pytest.FixtureRequest) -> bool:
    """编排开关（默认开启；scenario 7 用间接参数化关闭）。

    开关必须在测试 app 构建**之前**落定，所以它走 client fixture 的依赖链，
    而不是测试体内再改环境变量（测试体里改已经晚于路由注册）。
    """
    return bool(getattr(request, "param", True))


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, orchestrationEnabled: bool
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv(
        "ENABLE_AGENT_ORCHESTRATION", "true" if orchestrationEnabled else "false"
    )
    getSettings.cache_clear()
    crypto.resetFernet()
    resetEmbeddingClientCache()
    try:
        async for apiClient in _pg_support.pgApiClient():
            yield apiClient
    finally:
        getSettings.cache_clear()


class _ScriptedLlm(BaseLlmClient):
    """One provider double per test; every factory use records the fixed config."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.factoryConfigs: list[tuple[int, str]] = []
        self.calls: list[list[LlmMessage]] = []

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
        if not self._responses:
            raise AssertionError("unexpected extra LLM call")
        return LlmResponse(
            content=self._responses.pop(0),
            modelName="acceptance-fixed-model",
            promptTokens=_LLM_PROMPT_TOKENS,
            completionTokens=_LLM_COMPLETION_TOKENS,
            totalTokens=_LLM_PROMPT_TOKENS + _LLM_COMPLETION_TOKENS,
        )

    def completeStream(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("orchestration acceptance does not stream")

    async def complete_with_tools(self, *args: Any, **kwargs: Any):
        raise NotImplementedError("planner uses complete, not native tool calling")

    async def close(self) -> None:
        return None


def _installLlm(
    monkeypatch: pytest.MonkeyPatch, responses: list[str]
) -> _ScriptedLlm:
    scripted = _ScriptedLlm(responses)

    def factory(config: Any) -> _ScriptedLlm:
        scripted.factoryConfigs.append((config.id, config.model_name))
        return scripted

    monkeypatch.setattr(agent_orchestration_metering, "createClient", factory)
    return scripted


def _stubTool(
    monkeypatch: pytest.MonkeyPatch,
    handlerRef: str,
    calls: list[dict[str, Any]],
    *,
    useLlm: bool = False,
) -> None:
    async def handler(session, args, context) -> ToolResult:
        calls.append({"handlerRef": handlerRef, "args": dict(args)})
        answer = f"{handlerRef} observation"
        if useLlm:
            llm = context.llm_factory(context.llm_config)
            response = await llm.complete(
                [LlmMessage(role="user", content="summarize this read-only result")]
            )
            answer = response.content
        return ToolResult(data={"handlerRef": handlerRef, "text": answer}, answer=answer)

    monkeypatch.setitem(agent_tools.BUILTIN_HANDLERS, handlerRef, handler)


async def _createAgent(
    client: AsyncClient,
    *,
    policies: list[dict[str, Any]] | None = None,
) -> None:
    defaultPolicies = [
        {"data_object": "WIKI_PAGE", "permission": "read", "data_layer": "DIM"}
    ]
    response = await client.post(
        "/api/v1/agents",
        headers=OWNER_HEADERS,
        json={
            "agent_code": AGENT_CODE,
            "agent_name": "Orchestration acceptance agent",
            "description": "real PostgreSQL acceptance fixture",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM"],
            "status": "active",
            "policies": defaultPolicies if policies is None else policies,
        },
    )
    assert response.status_code == 201, response.text


async def _createTool(client: AsyncClient, name: str, handlerRef: str) -> None:
    response = await client.post(
        "/api/v1/agent-tools",
        headers=ADMIN_HEADERS,
        json={
            "name": name,
            "description": f"{name} read-only acceptance tool",
            "data_object": "WIKI_PAGE",
            "data_layers": ["DIM"],
            "input_schema": QUERY_SCHEMA,
            "handler_kind": "BUILTIN",
            "handler_ref": handlerRef,
            "arg_extractor_kind": "wiki_text",
        },
    )
    assert response.status_code == 201, response.text


async def _createModel(
    client: AsyncClient,
    name: str = "acceptance-fixed-model",
    *,
    costPer1kInput: float = 0.001,
    costPer1kOutput: float = 0.002,
) -> int:
    response = await client.post(
        "/api/v1/models",
        headers=ADMIN_HEADERS,
        json={
            "model_name": name,
            "provider": "openai",
            "cost_per_1k_input": costPer1kInput,
            "cost_per_1k_output": costPer1kOutput,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _createRunFixture(
    client: AsyncClient,
    tools: list[tuple[str, str]],
    *,
    policies: list[dict[str, Any]] | None = None,
    limits: dict[str, Any] | None = None,
) -> tuple[str, int]:
    await _createAgent(client, policies=policies)
    for name, handlerRef in tools:
        await _createTool(client, name, handlerRef)
    profile = await client.post(
        PROFILES_PATH,
        headers=OWNER_HEADERS,
        json={
            "agent_code": AGENT_CODE,
            "instructions": "Use only bound read-only tools, then finish.",
            "tool_names": [name for name, _ in tools],
            "default_max_iterations": 6,
            "default_max_tool_calls": 6,
            "default_max_cost_usd": 1.0,
            "default_timeout_seconds": 120,
            "max_observation_chars": 5000,
        },
    )
    assert profile.status_code == 201, profile.text
    toggled = await client.post(
        f"{PROFILES_PATH}/{AGENT_CODE}/toggle",
        headers=OWNER_HEADERS,
        json={"enabled": True, "revision": profile.json()["revision"]},
    )
    assert toggled.status_code == 200, toggled.text
    modelId = await _createModel(client)
    payload: dict[str, Any] = {
        "agentCode": AGENT_CODE,
        "input": "answer the acceptance question",
        "modelConfigId": modelId,
    }
    if limits is not None:
        payload["limits"] = limits
    created = await client.post(RUNS_PATH, headers=OWNER_HEADERS, json=payload)
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "QUEUED"
    return created.json()["runId"], modelId


async def _getRun(client: AsyncClient, runId: str) -> dict[str, Any]:
    response = await client.get(f"{RUNS_PATH}/{runId}", headers=OWNER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _assertTestEngine() -> None:
    """守卫：全局会话工厂必须指向**隔离测试库**，绝不写到业务库。

    ``_pg_support.pgApiClient()`` 会临时把 ``dbModule._sessionFactory`` 换成测试
    引擎；一旦在 fixture 生命周期之外拿到工厂，兜底路径会落到
    ``settings.databaseUrl``（业务库）——所以每次开会话前都断言一次库名。
    """
    engine = dbModule._engine
    assert engine is not None, "全局会话工厂未指向测试引擎"
    expected = make_url(_pg_support.resolveTestDatabaseUrl())
    assert engine.url.database == expected.database, (
        f"会话工厂指向非测试库：{engine.url.database} != {expected.database}"
    )


@asynccontextmanager
async def _testSession() -> AsyncIterator[AsyncSession]:
    """打开一个指向隔离测试库的会话（先断言引擎确实是测试库）。"""
    _assertTestEngine()
    async with dbModule.getSessionFactory()() as session:
        yield session


def _worker(
    workerId: str,
    *,
    runner: Any | None = None,
    store: AgentOrchestrationStore | None = None,
) -> AgentOrchestrationWorker:
    _assertTestEngine()
    actualRunner = runner or AgentOrchestrationRunner(store=store)
    return AgentOrchestrationWorker(
        getSettings(),
        dbModule.getSessionFactory(),
        actualRunner,
        workerId=workerId,
        store=store,
    )


async def _executeNext(
    workerId: str = "acceptance-worker",
    *,
    store: AgentOrchestrationStore | None = None,
) -> None:
    assert await _worker(workerId, store=store).runOnce() is True


async def _runRow(runId: str) -> AgentOrchestrationRun:
    async with _testSession() as session:
        row = (
            await session.execute(
                select(AgentOrchestrationRun).where(AgentOrchestrationRun.run_id == runId)
            )
        ).scalar_one()
        await session.refresh(row)
        return row


async def _usageRows(runId: str) -> list[AgentOrchestrationUsage]:
    async with _testSession() as session:
        return list(
            (
                await session.execute(
                    select(AgentOrchestrationUsage)
                    .join(
                        AgentOrchestrationRun,
                        AgentOrchestrationUsage.run_id == AgentOrchestrationRun.id,
                    )
                    .where(AgentOrchestrationRun.run_id == runId)
                    .order_by(AgentOrchestrationUsage.id)
                )
            )
            .scalars()
            .all()
        )


async def _orchestrationRunCount(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(AgentOrchestrationRun)))
        .scalar_one()
    )


async def _expectedRowCosts(
    client: AsyncClient,
    modelConfigId: int,
    usage: list[AgentOrchestrationUsage],
) -> list[Decimal]:
    """按模型配置声明的单价 + 落库 token 数重算每条 usage 行的应有成本。

    单价从 API 读回（`costPer1KInput` / `costPer1KOutput`，语义为「每 1K token
    的价格」），token 数取落库 usage 行本身——因此这是一条独立于生产计费代码的
    口径核对：token 取错、单价取错、或成本来自别的来源都会让断言失败。
    """
    response = await client.get(f"/api/v1/models/{modelConfigId}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    config = response.json()
    inputPrice = Decimal(str(config["costPer1KInput"]))
    outputPrice = Decimal(str(config["costPer1KOutput"]))
    expected: list[Decimal] = []
    for row in usage:
        assert row.cached_tokens is None, "用例前提：无 cache 命中 token"
        expected.append(
            (
                Decimal(row.prompt_tokens) * inputPrice
                + Decimal(row.completion_tokens) * outputPrice
            )
            / Decimal(1000)
        )
    return expected


async def test_scenario_1_two_tools_finish_steps_and_usage(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10.2.1: profile -> bindings -> API run -> Worker -> steps -> success."""
    handlerCalls: list[dict[str, Any]] = []
    _stubTool(monkeypatch, "wiki_search", handlerCalls)
    _stubTool(monkeypatch, "wiki_read", handlerCalls)
    scripted = _installLlm(monkeypatch, [CALL_A, CALL_B, FINISH])
    runId, _ = await _createRunFixture(
        client, [("acceptance_a", "wiki_search"), ("acceptance_b", "wiki_read")]
    )

    await _executeNext()

    run = await _getRun(client, runId)
    assert run["status"] == "SUCCEEDED"
    assert run["finalAnswer"] == "acceptance final answer"
    assert (run["iterationCount"], run["toolCallCount"]) == (3, 2)
    assert (run["promptTokens"], run["completionTokens"], run["totalTokens"]) == (33, 21, 54)
    assert run["costSource"] == "ESTIMATED" and run["costComplete"] is True
    steps = await client.get(f"{RUNS_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    assert steps.status_code == 200, steps.text
    body = steps.json()
    assert [step["kind"] for step in body] == ["PLAN", "TOOL", "PLAN", "TOOL", "PLAN"]
    assert [step["toolName"] for step in body if step["kind"] == "TOOL"] == [
        "acceptance_a",
        "acceptance_b",
    ]
    assert [call["handlerRef"] for call in handlerCalls] == ["wiki_search", "wiki_read"]
    assert len(scripted.calls) == 3
    assert len(await _usageRows(runId)) == 3


async def test_scenario_2_ask_pause_resume_queue_and_succeed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10.2.2: ASK_USER pauses; HTTP resume requeues and Worker finishes."""
    _installLlm(monkeypatch, [ASK_USER, FINISH])
    runId, _ = await _createRunFixture(client, [("acceptance_a", "wiki_search")])
    await _executeNext()
    paused = await _getRun(client, runId)
    assert paused["status"] == "PAUSED" and paused["pauseCode"] == "NEEDS_INPUT"

    resumed = await client.post(
        f"{RUNS_PATH}/{runId}/resume",
        headers=OWNER_HEADERS,
        json={"input": "supplier S-001"},
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["status"] == "QUEUED"
    assert "supplier S-001" in resumed.json()["question"]
    await _executeNext("resume-worker")

    finished = await _getRun(client, runId)
    assert finished["status"] == "SUCCEEDED"
    assert finished["finalAnswer"] == "acceptance final answer"
    assert finished["pauseCode"] is None
    assert finished["iterationCount"] == 2
    assert (await _runRow(runId)).lease_owner is None


async def test_scenario_3_acl_pause_zero_handler_and_owner_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10.2.3: deny-by-default ACL pauses before TOOL creation/handler call."""
    handlerCalls: list[dict[str, Any]] = []
    _stubTool(monkeypatch, "wiki_search", handlerCalls)
    _installLlm(monkeypatch, [CALL_A])
    runId, _ = await _createRunFixture(
        client, [("acceptance_a", "wiki_search")], policies=[]
    )

    await _executeNext()

    run = await _getRun(client, runId)
    assert run["status"] == "PAUSED" and run["pauseCode"] == "PERMISSION_DENIED"
    assert run["errorMessage"] is None
    assert handlerCalls == []
    steps = await client.get(f"{RUNS_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    assert steps.status_code == 200 and [row["kind"] for row in steps.json()] == ["PLAN"]
    outsider = await client.get(f"{RUNS_PATH}/{runId}", headers=OUTSIDER_HEADERS)
    assert outsider.status_code == 404
    assert outsider.json()["error"] == "agent run not found"
    assert "WIKI_PAGE" not in outsider.text and "policy" not in outsider.text.lower()


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def runClaimed(self, runId: int, workerId: str) -> None:
        self.calls.append((runId, workerId))


async def test_scenario_4_api_run_has_one_concurrent_lease_owner(
    client: AsyncClient,
) -> None:
    """10.2.4 API complement; the complete existing 100-round race suite is mandatory."""
    runId, _ = await _createRunFixture(client, [("acceptance_a", "wiki_search")])
    runner = _RecordingRunner()

    claimed = await asyncio.gather(
        _worker("race-a", runner=runner).runOnce(),
        _worker("race-b", runner=runner).runOnce(),
    )

    assert sorted(claimed) == [False, True]
    assert len(runner.calls) == 1 and runner.calls[0][1] in {"race-a", "race-b"}
    persisted = await _runRow(runId)
    assert persisted.status == "RUNNING"
    assert persisted.lease_owner == runner.calls[0][1]
    assert persisted.lease_until is not None and persisted.lease_until > datetime.now(UTC)


class _SimulatedProcessCrash(BaseException):
    pass


class _PreCommitCrashStore(AgentOrchestrationStore):
    async def completeToolStep(self, *args: Any, **kwargs: Any) -> None:
        raise _SimulatedProcessCrash("after handler, before output commit")


async def test_scenario_5_api_run_replays_one_step_after_precommit_crash(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10.2.5 API complement; Task 9's complete crash matrix is also mandatory."""
    handlerCalls: list[dict[str, Any]] = []
    _stubTool(monkeypatch, "wiki_search", handlerCalls)
    _installLlm(monkeypatch, [CALL_A, FINISH])
    runId, _ = await _createRunFixture(client, [("acceptance_a", "wiki_search")])

    with pytest.raises(_SimulatedProcessCrash):
        await _executeNext("crashed-worker", store=_PreCommitCrashStore())
    crashedSteps = await client.get(f"{RUNS_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    assert [row["kind"] for row in crashedSteps.json()] == ["PLAN", "TOOL"]
    assert crashedSteps.json()[1]["status"] == "RUNNING"
    assert len(handlerCalls) == 1

    async with _testSession() as session:
        await session.execute(
            update(AgentOrchestrationRun)
            .where(AgentOrchestrationRun.run_id == runId)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    await _executeNext("recovery-worker")

    finished = await _getRun(client, runId)
    assert finished["status"] == "SUCCEEDED" and finished["toolCallCount"] == 1
    steps = await client.get(f"{RUNS_PATH}/{runId}/steps", headers=OWNER_HEADERS)
    toolSteps = [row for row in steps.json() if row["kind"] == "TOOL"]
    assert len(toolSteps) == 1 and toolSteps[0]["stepNo"] == 1
    assert toolSteps[0]["inputJson"] == {"query": "alpha"}
    assert len(handlerCalls) == 2


async def _assertMeteringFacts(
    client: AsyncClient,
    run: dict[str, Any],
    usage: list[AgentOrchestrationUsage],
    modelId: int,
) -> None:
    """核对计量事实：固定模型、逐行 token、逐行成本、run 聚合成本。

    逐行 token 必须等于脚本化 LLM 声明的单次用量——这是**独立于落库行**的锚点：
    若某行把「总量」当成「本次用量」记，两行之和仍等于聚合值，下面的成本等式
    也会照样成立（假绿）。所以先按行钉死 token，再按「落库 token × 配置单价」
    重算成本——token 取错、单价取错、成本来源取错，三者都会让断言变红。
    """
    assert [row.caller_type for row in usage] == ["PLANNER", "TOOL"]
    assert {row.model_config_id for row in usage} == {modelId}
    assert {row.model_name for row in usage} == {"acceptance-fixed-model"}
    assert [(row.prompt_tokens, row.completion_tokens) for row in usage] == [
        (_LLM_PROMPT_TOKENS, _LLM_COMPLETION_TOKENS),
        (_LLM_PROMPT_TOKENS, _LLM_COMPLETION_TOKENS),
    ]
    expectedRowCosts = await _expectedRowCosts(client, modelId, usage)
    for row, expected in zip(usage, expectedRowCosts, strict=True):
        assert abs(row.cost - expected) <= _COST_TOLERANCE, (
            f"usage row {row.caller_type} 成本与「落库 token × 配置单价」不一致"
        )
    assert abs(Decimal(str(run["cost"])) - sum(expectedRowCosts)) <= _COST_TOLERANCE, (
        "run 聚合成本与 usage 行成本之和不一致"
    )


async def test_scenario_6_budget_fixed_model_and_tool_internal_metering(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10.2.6: persisted budgets and every Planner/TOOL call use one exact model."""
    handlerCalls: list[dict[str, Any]] = []
    _stubTool(monkeypatch, "supplier_risk", handlerCalls, useLlm=True)
    scripted = _installLlm(monkeypatch, [CALL_RISK, "tool narrative", FINISH])
    limits = {"maxIterations": 4, "maxToolCalls": 1, "maxCostUsd": 0.5, "timeoutSeconds": 90}
    runId, modelId = await _createRunFixture(
        client, [("acceptance_risk", "supplier_risk")], limits=limits
    )
    # decoy 模型单价**故意不同**：若成本算的是别的模型配置，成本等式立刻不成立
    # （只靠 model_config_id 断言无法区分「单价取自同一个数值的另一个模型」）。
    decoyId = await _createModel(
        client,
        "acceptance-decoy-model",
        costPer1kInput=0.5,
        costPer1kOutput=0.9,
    )
    assert decoyId != modelId

    await _executeNext()

    run = await _getRun(client, runId)
    assert run["status"] == "PAUSED" and run["modelConfigId"] == modelId
    assert run["pauseCode"] == "BUDGET_EXHAUSTED"
    assert run["pauseDetail"] == {"budget": "tool"}
    assert {key: run[key] for key in limits} == limits
    assert (run["iterationCount"], run["toolCallCount"]) == (1, 1)
    assert (run["promptTokens"], run["completionTokens"], run["totalTokens"]) == (22, 14, 36)
    assert run["costSource"] == "ESTIMATED" and run["costComplete"] is True
    usage = await _usageRows(runId)
    await _assertMeteringFacts(client, run, usage, modelId)
    assert scripted.factoryConfigs == [(modelId, "acceptance-fixed-model")] * 2
    assert len(scripted.calls) == 2
    assert len(handlerCalls) == 1


async def _seedLegacyEntityMapping() -> None:
    """种下 entity_mapping 行（外键指向 business_object.code）。

    测试库每例 TRUNCATE，业务对象行由集成 conftest 重新 seed；这里补的是
    供应商映射本身，供旧 run 端点的 supplier_360 handler 解析企业键。
    """
    async with _testSession() as session:
        session.add(
            EntityMapping(
                entity_type="SUPPLIER",
                enterprise_key=100001,
                enterprise_code="SUP000001",
                source_system=SourceSystem.ERP,
                source_key="V100001",
                source_code="V100001",
                match_rule=MatchRule.MDM_MASTER,
            )
        )
        await session.commit()


@pytest.mark.parametrize("orchestrationEnabled", [False], indirect=True)
async def test_scenario_7_disabled_routes_404_while_legacy_run_succeeds(
    client: AsyncClient,
) -> None:
    """10.2.7: disabled means unregistered new routes, not a changed legacy API."""
    legacyAgent = await client.post(
        "/api/v1/agents",
        headers=OWNER_HEADERS,
        json={
            "agent_code": AGENT_CODE,
            "agent_name": "Legacy control agent",
            "description": "disabled feature control",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM", "FEATURE"],
            "status": "active",
            "tool_name": "supplier_360",
            "policies": [
                {"data_object": "SUPPLIER", "permission": "read", "data_layer": "DIM"},
                {"data_object": "SUPPLIER", "permission": "read", "data_layer": "FEATURE"},
            ],
        },
    )
    assert legacyAgent.status_code == 201, legacyAgent.text
    await _seedLegacyEntityMapping()
    legacy = await client.post(
        f"/api/v1/agents/{AGENT_CODE}/run",
        headers=OWNER_HEADERS,
        json={"input": "供应商 100001 的 360° 视图"},
    )
    newProfiles = await client.get(PROFILES_PATH, headers=OWNER_HEADERS)
    newRuns = await client.get(RUNS_PATH, headers=OWNER_HEADERS)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["tool"] == "supplier_360"
    assert "runId" not in legacy.json() and "executedAt" in legacy.json()
    assert newProfiles.status_code == 404 and newProfiles.json() == {"detail": "Not Found"}
    assert newRuns.status_code == 404 and newRuns.json() == {"detail": "Not Found"}
    async with _testSession() as session:
        assert await _orchestrationRunCount(session) == 0


async def test_scenario_8_delete_guards_and_historical_agent_code(
    client: AsyncClient,
) -> None:
    """10.2.8: bound delete 409, unbound delete 204, hard delete preserves history."""
    runId, _ = await _createRunFixture(client, [("acceptance_a", "wiki_search")])
    await _createTool(client, "acceptance_unbound", "wiki_read")

    boundDelete = await client.delete("/api/v1/agent-tools/acceptance_a", headers=ADMIN_HEADERS)
    assert boundDelete.status_code == 409, boundDelete.text
    assert "agent_tool_in_use_orchestration" in boundDelete.text
    unboundDelete = await client.delete(
        "/api/v1/agent-tools/acceptance_unbound", headers=ADMIN_HEADERS
    )
    assert unboundDelete.status_code == 204 and unboundDelete.content == b""

    deleted = await client.delete(
        f"/api/v1/agents/{AGENT_CODE}?hard_delete=true", headers=ADMIN_HEADERS
    )
    assert deleted.status_code == 200, deleted.text
    historical = await _getRun(client, runId)
    assert historical["agentCode"] == AGENT_CODE
    listing = await client.get(
        f"{RUNS_PATH}?agentCode={AGENT_CODE}", headers=OWNER_HEADERS
    )
    assert listing.status_code == 200 and [row["runId"] for row in listing.json()] == [runId]
    persisted = await _runRow(runId)
    assert persisted.agent_id is None and persisted.agent_code == AGENT_CODE


def test_requiredRaceAndCrashSuitesExistInTree() -> None:
    """场景 4/5 的必跑套件必须在测试树里存在，且暴露其关键用例。

    只断言依赖存在（模块可导入 + 关键用例可调用），不重跑它们——两个套件与
    本文件在同一条 pytest 命令里执行。缺模块、改名、删用例都必须让本用例变红。
    """
    assert _REQUIRED_SUITES, "必跑套件清单为空——场景 4/5 的依赖网会空集通过"
    for moduleName, functionNames in _REQUIRED_SUITES.items():
        try:
            module = importlib.import_module(moduleName)
        except ImportError as exc:
            raise AssertionError(
                f"必跑套件 {moduleName} 不存在（{exc}）——"
                "删除/改名会让场景 4/5 的深度静默退化"
            ) from exc
        for functionName in functionNames:
            function = getattr(module, functionName, None)
            assert function is not None, (
                f"必跑套件 {moduleName} 缺少必需用例 {functionName}"
                "（删除/改名会让场景 4/5 的深度静默退化）"
            )
            assert callable(function), f"{moduleName}.{functionName} 不可调用"
