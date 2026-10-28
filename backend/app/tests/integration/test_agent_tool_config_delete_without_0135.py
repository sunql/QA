"""I2 回归：`deleteTool` 在**没有 0135 那 5 张表**的库上不得 500。

场景：编排特性（`ENABLE_AGENT_ORCHESTRATION`）已上代码，但目标库还没升到
`0135_agent_orchestration` ⇒ `agent_orchestration_*` 5 张表不存在。此时
`DELETE /api/v1/agent-tools/{name}` 必须给出 **409 / 204**，而不是 500。

**「表不存在」是怎么造出来的（技术选择）**：
跨 session 的 `DROP TABLE` 会给该表拿到 ACCESS EXCLUSIVE 锁 ⇒ **另一个**
session 的 SELECT 不是「relation does not exist」，而是**阻塞**（挂死而不是
报错）。所以「表不存在」必须在**发出 DDL 的那个连接**上复现：

- **flag 打开**（service 链路）：直接在**同一个 session**（就是发 DDL 的那个）
  上调用 `deleteTool` ⇒ 「表不存在」的 ProgrammingError 与
  SAVEPOINT fail-soft 发生在同一条连接上 ⇒ DROP 全部 5 张表。
- **flag 关闭**（API 链路）：API 用的是**另一条连接** ⇒ 只能 DROP 不牵连
  `agent_definition` 的那几张（见 `_API_SAFE_TABLES` 的注释），否则 API 那条
  连接上的「遗留引用检查」会被锁住而**阻塞**（实测 wait_event=Lock/relation）。
  flag 关闭时 API **根本不查**编排表 ⇒ 请求正常走完（204 / 409）。

两条路径最后都 `rollback()`：PostgreSQL 的 DDL 是事务性的 ⇒ 表和数据自动复原。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError
from app.domain.models import AgentToolConfig
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.services.agent_tool_config_service import AgentToolConfigService

ADMIN_HEADERS = {
    "X-User-Id": "orch-no0135-admin",
    "X-User-Roles": "admin",
    "X-User-Departments": "IT",
}
OWNER_HEADERS = {
    "X-User-Id": "orch-no0135-owner",
    "X-User-Roles": "analyst",
    "X-User-Departments": "procurement",
}

_VALID_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "additionalProperties": False,
}

# 0135 建的这 5 张表；DROP 顺序按 FK 依赖（usage→step→run、binding→profile）
_ORCHESTRATION_TABLES = (
    "agent_orchestration_usage",
    "agent_orchestration_step",
    "agent_orchestration_binding",
    "agent_orchestration_profile",
    "agent_orchestration_run",
)

# API 链路（**另一条连接**）能安全 DROP 的表：`agent_orchestration_usage` /
# `agent_orchestration_step` / `agent_orchestration_binding` —— 它们都**不**直接
# 外键指向 `agent_definition`。
#
# 为什么不 DROP 全部 5 张：DROP 一张**带外键指向 agent_definition** 的表
# （profile / run）会让 PG 在 `agent_definition` 上取 ACCESS EXCLUSIVE 锁 ⇒
# API 那条连接上的 `SELECT ... FROM agent_definition`（遗留引用检查）不是报错而是
# **阻塞**（实测：pg_stat_activity 显示 wait_event=Lock/relation）⇒ 反而复现不出
# 「表不存在」。service 链路（同一个 session）没有这个限制 ⇒ 那两条用例 DROP 全部 5 张。
_API_SAFE_TABLES = (
    "agent_orchestration_usage",
    "agent_orchestration_step",
    "agent_orchestration_binding",
)


def _adminActor() -> CurrentUser:
    return CurrentUser(userId="orch-no0135-admin", roles=["admin"], departments=["IT"])


@pytest.fixture
def flagOff(monkeypatch):
    """编排特性关闭（出厂默认）：deleteTool 不查编排表。"""
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
    getSettings.cache_clear()
    yield
    getSettings.cache_clear()


@pytest.fixture
def flagOn(monkeypatch):
    """编排特性打开：deleteTool 会查编排表（库没迁移时须 fail-soft）。"""
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    yield
    getSettings.cache_clear()


async def _dropOrchestrationTables(
    session: AsyncSession, tables: tuple[str, ...] = _ORCHESTRATION_TABLES
) -> None:
    """在**当前 session 的事务里** DROP 这些表（未提交 ⇒ 别的连接看不见，但会被锁住）。"""
    await session.execute(text(f"DROP TABLE IF EXISTS {', '.join(tables)}"))


async def _createTool(client: AsyncClient, name: str) -> None:
    resp = await client.post(
        "/api/v1/agent-tools",
        headers=ADMIN_HEADERS,
        json={
            "name": name,
            "description": f"{name} read-only tool",
            "data_object": "WIKI_PAGE",
            "data_layers": ["DIM"],
            "input_schema": _VALID_SCHEMA,
            "handler_kind": "BUILTIN",
            "handler_ref": "wiki_search",
            "arg_extractor_kind": "wiki_text",
        },
    )
    assert resp.status_code == 201, resp.text


async def _warmToolRegistry(session: AsyncSession) -> None:
    """把新建的工具灌进 DB-backed registry 单例（Agent 绑定的校验读它）。"""
    agent_tool_config_registry.invalidate()
    await agent_tool_config_registry.warmUp(session)
    await session.commit()


@pytest.mark.asyncio
async def test_delete_tool_api_returns_204_when_orchestration_tables_missing(
    client: AsyncClient, dbSession: AsyncSession, flagOff
) -> None:
    """flag 关闭 + 编排表不存在 ⇒ 204（不是 500）。"""
    await _createTool(client, "no0135_free")

    await _dropOrchestrationTables(dbSession, _API_SAFE_TABLES)
    try:
        response = await client.delete(
            "/api/v1/agent-tools/no0135_free", headers=ADMIN_HEADERS
        )
        assert response.status_code == 204, response.text
        assert response.content == b""
    finally:
        await dbSession.rollback()


@pytest.mark.asyncio
async def test_delete_tool_api_returns_409_when_legacy_reference_exists(
    client: AsyncClient, dbSession: AsyncSession, flagOff
) -> None:
    """flag 关闭 + 编排表不存在 + 有历史引用 ⇒ 409（不是 500）。"""
    await _createTool(client, "no0135_ref")
    await _warmToolRegistry(dbSession)

    # 历史引用：AgentDefinition.tool_name 指向该工具
    resp = await client.post(
        "/api/v1/agents",
        headers=OWNER_HEADERS,
        json={
            "agent_code": "NO0135_LEGACY_AGENT",
            "agent_name": "no0135 legacy agent",
            "description": "legacy reference for the delete guard",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM"],
            "status": "active",
            "tool_name": "no0135_ref",
            "policies": [
                {
                    "data_object": "WIKI_PAGE",
                    "permission": "read",
                    "data_layer": "DIM",
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    await _dropOrchestrationTables(dbSession, _API_SAFE_TABLES)
    try:
        response = await client.delete(
            "/api/v1/agent-tools/no0135_ref", headers=ADMIN_HEADERS
        )
        assert response.status_code == 409, response.text
        detail = response.json().get("detail") or ""
        assert "NO0135_LEGACY_AGENT" in detail
    finally:
        await dbSession.rollback()


@pytest.mark.asyncio
async def test_delete_tool_service_fails_soft_when_orchestration_tables_missing(
    client: AsyncClient, dbSession: AsyncSession, flagOn
) -> None:
    """flag 打开 + 编排表不存在 ⇒ 降级为「无编排引用」，删除成功（204 等价）。

    「表不存在」与 fail-soft 必须在**同一条连接**上 ⇒ 直接用发 DDL 的那个
    `dbSession` 调 service（不走 API：API 用的是另一条连接，会被锁阻塞）。
    """
    await _createTool(client, "no0135_service")

    await _dropOrchestrationTables(dbSession)
    try:
        service = AgentToolConfigService()
        await service.deleteTool(dbSession, "no0135_service", _adminActor())

        # 204 等价：行被删掉了
        remaining = (
            await dbSession.execute(
                select(AgentToolConfig).where(
                    AgentToolConfig.name == "no0135_service"
                )
            )
        ).scalar_one_or_none()
        assert remaining is None

        # fail-soft 之后**同一个 session 仍然可用**（SAVEPOINT 回滚 ⇒ 事务没被中止）
        probe = (await dbSession.execute(text("SELECT 1"))).scalar_one()
        assert probe == 1
        # 写路径也可用（写进同一个事务 ⇒ 最后 rollback 复原，不污染共享测试库）
        await dbSession.execute(
            text(
                "INSERT INTO audit_outbox "
                "(event_type, entity_type, actor, payload, created_at) "
                "VALUES ('probe', 'probe', 'probe', '{}'::jsonb, now())"
            )
        )
    finally:
        # 关键：**只能 rollback**。commit 会把 DROP 提交掉 ⇒ 共享测试库少 5 张表。
        await dbSession.rollback()


@pytest.mark.asyncio
async def test_delete_tool_service_returns_conflict_when_legacy_reference_exists(
    client: AsyncClient, dbSession: AsyncSession, flagOn
) -> None:
    """flag 打开 + 编排表不存在 + 有历史引用 ⇒ 409 等价（ConflictError），不是 500。"""
    await _createTool(client, "no0135_service_ref")
    await _warmToolRegistry(dbSession)

    resp = await client.post(
        "/api/v1/agents",
        headers=OWNER_HEADERS,
        json={
            "agent_code": "NO0135_SERVICE_AGENT",
            "agent_name": "no0135 service agent",
            "description": "legacy reference for the delete guard",
            "data_domains": ["PROCUREMENT"],
            "data_layers": ["DIM"],
            "status": "active",
            "tool_name": "no0135_service_ref",
            "policies": [
                {
                    "data_object": "WIKI_PAGE",
                    "permission": "read",
                    "data_layer": "DIM",
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    await _dropOrchestrationTables(dbSession)
    try:
        service = AgentToolConfigService()
        with pytest.raises(ConflictError) as exc_info:
            await service.deleteTool(dbSession, "no0135_service_ref", _adminActor())
        assert exc_info.value.referencing_agents == ["NO0135_SERVICE_AGENT"]

        # session 仍然可用
        probe = (await dbSession.execute(text("SELECT 1"))).scalar_one()
        assert probe == 1
    finally:
        await dbSession.rollback()
