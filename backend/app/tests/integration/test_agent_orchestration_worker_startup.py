"""C1 回归：Worker 进程启动预热（真实注册表 + 真实 PostgreSQL）。

背景：Worker 是**独立进程**，不经过 API 的 lifespan ⇒ API 里那一次
``agent_tool_config_registry.warmUp()`` 对 Worker 无效。冷启动时
``AgentToolConfigRegistry.get()`` 抛 ``RuntimeError``，而 ``RuntimeError`` 不在
``INFRASTRUCTURE_EXCEPTIONS`` 里 ⇒ 编排 run 一走到工具解析就必然判 FAILED。

**为什么不能用替身**：``app/tests/unit/test_agent_orchestration_tool_executor.py``
里的 ``_FakeRegistry`` 没有 ``_loaded`` 守卫，替身形态根本复现不出这个缺陷。
本文件用**真实的** ``AgentToolConfigRegistry`` + **真实 PostgreSQL**。

**为什么不能靠集成 conftest 的预热**：``app/tests/integration/conftest.py`` 的
autouse fixture ``warmAgentCaches`` 每个测试前都会把**模块单例**
``agent_tool_config_registry`` 预热好 ⇒ 只 ``get()`` 一下的测试在**修之前也会绿**
（假绿）。本文件一律用**自己 new 出来的、未预热的真实实例**。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.config import getSettings
from app.infrastructure import database as dbModule
from app.infrastructure.security import crypto
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_orchestration_tool_executor import AgentOrchestrationToolExecutor
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.agent_tool_config_registry import AgentToolConfigRegistry
from app.services.feature_rule_registry import FeatureRuleRegistry
from app.tests import _pg_support
from app.workers import agent_orchestration_worker as workerModule
from app.workers.agent_orchestration_worker import AgentOrchestrationWorker

OWNER_HEADERS = {
    "X-User-Id": "worker-startup-owner",
    "X-User-Roles": "analyst",
    "X-User-Departments": "procurement",
}
ADMIN_HEADERS = {
    "X-User-Id": "worker-startup-admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
AGENT_CODE = "WORKER_STARTUP_AGENT"
TOOL_NAME = "worker_startup_tool"
PROFILES_PATH = "/api/v1/agent-orchestrations"
RUNS_PATH = "/api/v1/agent-runs"
QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """编排开关打开的真实 PG 客户端（路由注册依赖开关）。"""
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    crypto.resetFernet()
    try:
        async for apiClient in _pg_support.pgApiClient():
            yield apiClient
    finally:
        getSettings.cache_clear()


async def _createAgentAndRun(client: AsyncClient) -> str:
    """建 Agent / 工具 / 编排 profile / 模型 / run，返回 runId。"""
    agent = await client.post(
        "/api/v1/agents",
        headers=OWNER_HEADERS,
        json={
            "agent_code": AGENT_CODE,
            "agent_name": "Worker startup agent",
            "description": "C1 回归夹具",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM"],
            "status": "active",
            "policies": [
                {"data_object": "WIKI_PAGE", "permission": "read", "data_layer": "DIM"}
            ],
        },
    )
    assert agent.status_code == 201, agent.text

    tool = await client.post(
        "/api/v1/agent-tools",
        headers=ADMIN_HEADERS,
        json={
            "name": TOOL_NAME,
            "description": "只读工具（C1 回归夹具）",
            "data_object": "WIKI_PAGE",
            "data_layers": ["DIM"],
            "input_schema": QUERY_SCHEMA,
            "handler_kind": "BUILTIN",
            "handler_ref": "wiki_search",
            "arg_extractor_kind": "wiki_text",
        },
    )
    assert tool.status_code == 201, tool.text

    profile = await client.post(
        PROFILES_PATH,
        headers=OWNER_HEADERS,
        json={
            "agent_code": AGENT_CODE,
            "instructions": "只调用绑定的只读工具。",
            "tool_names": [TOOL_NAME],
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

    model = await client.post(
        "/api/v1/models",
        headers=ADMIN_HEADERS,
        json={
            "model_name": "worker-startup-model",
            "provider": "openai",
            "cost_per_1k_input": 0.001,
            "cost_per_1k_output": 0.002,
            "is_active": True,
        },
    )
    assert model.status_code == 201, model.text

    created = await client.post(
        RUNS_PATH,
        headers=OWNER_HEADERS,
        json={
            "agentCode": AGENT_CODE,
            "input": "回答编排问题",
            "modelConfigId": model.json()["id"],
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "QUEUED"
    return str(created.json()["runId"])


async def test_cold_registry_raises_runtime_error() -> None:
    """冷态：新建的、未预热的真实注册表 ``get()`` 抛 RuntimeError。

    这条断言把「Worker 进程冷启动」的形态钉死：注册表没预热就是响亮失败，
    不是静默返回 None。
    """
    coldRegistry = AgentToolConfigRegistry()

    with pytest.raises(RuntimeError) as excinfo:
        coldRegistry.get("wiki_search")

    assert "未 warmUp" in str(excinfo.value)


async def test_worker_warm_up_makes_real_registry_usable() -> None:
    """预热后：worker 的预热函数让**同一个**真实注册表可以 ``get()``。"""
    freshRegistry = AgentToolConfigRegistry()
    freshFeatureRegistry = FeatureRuleRegistry()

    with pytest.raises(RuntimeError):
        freshRegistry.get("wiki_search")

    await workerModule.warmUpWorkerCaches(
        dbModule.getSessionFactory(),
        registry=freshRegistry,
        featureRegistry=freshFeatureRegistry,
    )

    tool = freshRegistry.get("wiki_search")
    assert tool is not None
    assert tool.name == "wiki_search"
    # supplier_360 / supplier_risk handler 内部依赖 feature_rule_registry：
    # 冷态同样抛 RuntimeError ⇒ 预热必须一起覆盖它。
    assert isinstance(
        freshFeatureRegistry.getEnabledRules("SUPPLIER", "FEATURE", "RISK"), list
    )


async def test_prepare_reaches_tool_resolution_with_warmed_registry(
    client: AsyncClient,
) -> None:
    """工具解析路径：``prepare()`` 得到的是工具/ACL 判定，不是 RuntimeError。

    走完整 ``prepare()`` 上下文（真实 Agent + profile + binding + run + 租约），
    而不是退到 ``_resolveTool()``。同一段代码先在**冷态**跑一次（C1 的原始
    症状：RuntimeError），再在**预热后**跑一次（工具/ACL 判定）。
    """
    runId = await _createAgentAndRun(client)
    freshRegistry = AgentToolConfigRegistry()
    store = AgentOrchestrationStore()
    workerId = "worker-startup-test"
    now = datetime.now(UTC)
    async with dbModule.getSessionFactory()() as session:
        claimed = await store.claimNext(session, workerId, 60, now)
        assert claimed is not None
        assert claimed.run_id == runId

    def _buildExecutor() -> AgentOrchestrationToolExecutor:
        return AgentOrchestrationToolExecutor(
            executionGuard=store,
            toolRegistry=freshRegistry,
            runtime=AgentRuntimeService(),
        )

    # 1) 冷态：C1 的症状 —— prepare() 抛 RuntimeError（而不是工具/ACL 判定）
    async with dbModule.getSessionFactory()() as session:
        with pytest.raises(RuntimeError) as coldError:
            await _buildExecutor().prepare(
                session, claimed, TOOL_NAME, {"query": "alpha"}, workerId, now
            )
        assert "未 warmUp" in str(coldError.value)
        await session.rollback()

    # 2) 预热后：同样的调用得到工具/ACL 判定
    await workerModule.warmUpWorkerCaches(
        dbModule.getSessionFactory(), registry=freshRegistry
    )
    async with dbModule.getSessionFactory()() as session:
        prepared = await _buildExecutor().prepare(
            session, claimed, TOOL_NAME, {"query": "alpha"}, workerId, now
        )

    assert prepared.tool.name == TOOL_NAME
    assert prepared.args == {"query": "alpha"}


async def test_run_calls_the_warm_up(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 的 ``run()`` 真的调用了预热（不是「有个函数但没人调」）。"""
    calls: list[Any] = []

    async def _recordingWarmUp(sessionFactory: Any) -> None:
        calls.append(sessionFactory)

    monkeypatch.setattr(workerModule, "warmUpWorkerCaches", _recordingWarmUp)

    class _NoopRunner:
        async def runClaimed(self, runId: int, workerId: str) -> None:
            raise AssertionError("预热失败时不该走到 runner")

    worker = AgentOrchestrationWorker(
        getSettings(),
        dbModule.getSessionFactory(),
        _NoopRunner(),
        workerId="worker-startup-probe",
    )
    worker.requestStop()
    await worker.run()

    assert len(calls) == 1
