"""编排/隔离测试的共享 support（供多个集成套件复用）。

背景：这些 helper 原先定义在 `app/tests/integration/test_agent_runtime_api.py`
内部，其它套件用 `from ... import _私有名` 跨模块引用。跨模块引用私有名一旦
目标文件被改名/删除，失败表现为**收集期 collection error**（而不是一条可读的
断言失败），排查成本高。抽到本模块后：

- 依赖关系显式：谁提供什么，一眼可见；
- 失败形态可控：模块缺失是 import 错误，但不再牵动「某个测试文件是不是还叫
  这个名字」这种隐式耦合；
- 各套件只保留自己真正关心的用例与断言。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.agent_orchestration_models import AgentOrchestrationRun
from app.infrastructure.llm.embedding_provider_factory import resetEmbeddingClientCache
from app.infrastructure.security import crypto
from app.services.agent_orchestration_run_service import AgentOrchestrationRunService
from app.services.agent_orchestration_runner import AgentOrchestrationRunner
from app.services.agent_runtime_service import AgentRuntimeService
from app.tests import _pg_support

# 编排配置/运行 API 的路径（与 main.py 的 include_router 前缀一致）。
ORCHESTRATION_PROFILE_PATH = "/api/v1/agent-orchestrations"

# 建工具/建 profile 需要 admin 权限（这两个端点只对 admin 开放）。
ORCH_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_ORCHESTRATION_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}


async def orchestrationClient(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """编排开关版 client 的**实现**（供各套件的 client fixture 复用）。

    开关只影响「哪些路由存在」，不影响旧端点行为；物理隔离断言需要**真实存在**
    的编排 profile（新建 profile 只能走公开 API，而新路由在开关关闭时不注册）。
    """
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    crypto.resetFernet()
    resetEmbeddingClientCache()
    try:
        async for apiClient in _pg_support.pgApiClient():
            yield apiClient
    finally:
        getSettings.cache_clear()


async def createOrchestrationTool(
    client: AsyncClient, name: str, handlerRef: str
) -> None:
    """经公开 API 建一个**编排可用**的只读工具（严格 object schema 是编排绑定前提）。"""
    response = await client.post(
        "/api/v1/agent-tools",
        headers=ORCH_ADMIN_HEADERS,
        json={
            "name": name,
            "description": f"{name} 编排隔离用只读工具",
            "data_object": "WIKI_PAGE",
            "data_layers": ["DIM"],
            "input_schema": _ORCHESTRATION_TOOL_SCHEMA,
            "handler_kind": "BUILTIN",
            "handler_ref": handlerRef,
            "arg_extractor_kind": "wiki_text",
        },
    )
    assert response.status_code == 201, response.text


async def createOrchestrationProfile(
    client: AsyncClient, agentCode: str, toolNames: list[str]
) -> dict[str, Any]:
    """经公开 API 建一个**已启用**的多工具编排 profile（并回读启用后的状态）。"""
    created = await client.post(
        ORCHESTRATION_PROFILE_PATH,
        headers=ORCH_ADMIN_HEADERS,
        json={
            "agentCode": agentCode,
            "instructions": "仅使用绑定的只读工具，然后结束。",
            "toolNames": toolNames,
        },
    )
    assert created.status_code == 201, created.text
    toggled = await client.post(
        f"{ORCHESTRATION_PROFILE_PATH}/{agentCode}/toggle",
        headers=ORCH_ADMIN_HEADERS,
        json={"enabled": True, "revision": created.json()["revision"]},
    )
    assert toggled.status_code == 200, toggled.text
    body = toggled.json()
    assert body["enabled"] is True
    assert [binding["toolName"] for binding in body["bindings"]] == toolNames
    return body


async def orchestrationRunCount(dbSession: AsyncSession) -> int:
    """编排 run 表的行数（隔离断言用它证明「旧链路没有偷偷排编排 run」）。"""
    return int(
        (
            await dbSession.execute(
                select(func.count()).select_from(AgentOrchestrationRun)
            )
        ).scalar_one()
    )


def spyOrchestrationRunner(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, str]]:
    """给新编排 runner 装间谍：任何一次 runClaimed 调用都会被记录。

    间谍**委托真实实现**（call-through），因此即使被调用也不会把 runner 变成
    空操作——否则「断言从未被调用」会在别处复用时变成假绿。
    """
    calls: list[tuple[int, str]] = []
    original = AgentOrchestrationRunner.runClaimed

    async def spyRunClaimed(self: Any, runId: int, workerId: str) -> None:
        calls.append((runId, workerId))
        await original(self, runId, workerId)

    monkeypatch.setattr(AgentOrchestrationRunner, "runClaimed", spyRunClaimed)
    return calls


def spyOrchestrationRunCreation(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """给**编排 run 创建入口**（``AgentOrchestrationRunService.createRun``）装间谍。

    旧链路若被改写成「顺手排一次编排 run」，落点就是这里（而不是
    ``runClaimed``——后者只由 Worker 调用，旧链路根本到不了）。间谍同样委托
    真实实现。
    """
    calls: list[str] = []
    original = AgentOrchestrationRunService.createRun

    async def spyCreateRun(self: Any, *args: Any, **kwargs: Any) -> Any:
        dto = args[1] if len(args) > 1 else kwargs.get("dto")
        calls.append(getattr(dto, "agent_code", "<unknown>"))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AgentOrchestrationRunService, "createRun", spyCreateRun)
    return calls


def spyOrchestration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[int, str]], list[str]]:
    """同时装上「编排 runner 调用」与「编排 run 创建」两个间谍。"""
    return (
        spyOrchestrationRunner(monkeypatch),
        spyOrchestrationRunCreation(monkeypatch),
    )


def spyLegacyRuntime(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    """给旧 AgentRuntimeService.run 装间谍（记录 agent_code 与输入）。"""
    calls: list[tuple[str, str]] = []
    original = AgentRuntimeService.run

    async def spyRun(
        self: Any,
        session: AsyncSession,
        agent_code: str,
        input_text: str,
        **kwargs: Any,
    ):
        calls.append((agent_code, input_text))
        return await original(self, session, agent_code, input_text, **kwargs)

    monkeypatch.setattr(AgentRuntimeService, "run", spyRun)
    return calls
