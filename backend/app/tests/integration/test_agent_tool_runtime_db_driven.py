"""运行时应从 DB 配置读取工具；cache miss → reload_one；enabled=false → 409。

T13: 验证 AgentRuntimeService._resolveTool 完全 DB-driven + cache invalidation 行为。
与 test_agent_runtime_api.py 区别：后者走完整 API 链路验证业务功能；
本文件直接对 _resolveTool + registry 做白盒测试，验证 SSOT（DB → registry → runtime）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError
from app.domain.models import AgentToolConfig
from app.domain.schemas import AgentToolConfigCreate
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.services.agent_tool_config_service import AgentToolConfigService
from app.services.agent_runtime_service import AgentRuntimeService

_ADMIN = CurrentUser(userId="admin", roles=("admin",), departments=("IT",))


_TOOL_SEEDS = [
    {
        "name": "supplier_360",
        "description": "查询单供应商 360° 视图",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_360",
        "arg_extractor_kind": "supplier_key",
        "enabled": True,
    },
    {
        "name": "supplier_risk",
        "description": "评估单供应商风险等级",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "FEATURE"],
        "handler_kind": "BUILTIN",
        "handler_ref": "supplier_risk",
        "arg_extractor_kind": "supplier_risk_key",
        "enabled": True,
    },
    {
        "name": "graph_traverse",
        "description": "供应链链路推理",
        "data_object": "SUPPLIER",
        "data_layers": ["DIM", "DWD"],
        "handler_kind": "BUILTIN",
        "handler_ref": "graph_traverse",
        "arg_extractor_kind": "supplier_graph_key",
        "enabled": True,
    },
]


@pytest.fixture(autouse=True)
async def _warmToolRegistry(dbSession) -> None:
    """每个测试前：seed 3 tool configs → warmUp registry + binding cache。"""
    service = AgentToolConfigService()
    for seed in _TOOL_SEEDS:
        await service.upsertSeed(dbSession, seed["name"], seed)
    await dbSession.commit()
    agent_tool_config_registry.invalidate()
    await agent_tool_config_registry.warmUp(dbSession)
    agent_binding_cache.invalidate()
    await agent_binding_cache.warmUp(dbSession)
    yield
    agent_tool_config_registry.invalidate()
    agent_binding_cache.invalidate()


class TestRuntimeResolveTool:
    async def test_runtime_uses_db_config(self, dbSession) -> None:
        """运行时 _resolveTool 应从 registry（已 warmUp）解析工具。"""
        svc = AgentRuntimeService()
        tool = await svc._resolveTool(dbSession, "supplier_360")
        assert tool.name == "supplier_360"
        assert tool.data_object == "SUPPLIER"
        assert "DIM" in tool.data_layers
        assert "FEATURE" in tool.data_layers

    async def test_disabled_tool_raises_409(self, dbSession) -> None:
        """工具 enabled=False 时，runtime 应拒绝（reload_one 后 tool 仍为 None）。"""
        # 先 invalidate 让下次 reload_one 真的查 DB
        await dbSession.execute(
            update(AgentToolConfig)
            .where(AgentToolConfig.name == "supplier_360")
            .values(enabled=False)
        )
        await dbSession.commit()
        agent_tool_config_registry.invalidate("supplier_360")

        svc = AgentRuntimeService()
        with pytest.raises(ConflictError):
            await svc._resolveTool(dbSession, "supplier_360")

    async def test_new_tool_effective_after_reload(self, dbSession) -> None:
        """新创建的工具在下一调用后立即生效（reload_one）。"""
        new_tool = AgentToolConfigCreate(
            name="runtime_new",
            description="runtime-test new",
            data_object="SUPPLIER",
            data_layers=["DIM", "FEATURE"],
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
            arg_extractor_kind="supplier_key",
        )
        await AgentToolConfigService().createTool(dbSession, new_tool, _ADMIN)
        await dbSession.commit()
        # runtime 应能解析出新工具（registry miss → reload_one → DB 命中）
        svc = AgentRuntimeService()
        tool = await svc._resolveTool(dbSession, "runtime_new")
        assert tool.name == "runtime_new"

    async def test_data_object_change_effective_after_invalidate(self, dbSession) -> None:
        """改 data_object → invalidate → 下一调用读到新值。"""
        await dbSession.execute(
            update(AgentToolConfig)
            .where(AgentToolConfig.name == "supplier_360")
            .values(data_object="VENDOR")
        )
        await dbSession.commit()
        agent_tool_config_registry.invalidate("supplier_360")

        svc = AgentRuntimeService()
        tool = await svc._resolveTool(dbSession, "supplier_360")
        assert tool.data_object == "VENDOR"

    async def test_unknown_tool_raises_409(self, dbSession) -> None:
        """不存在的工具名 → ConflictError。"""
        svc = AgentRuntimeService()
        with pytest.raises(ConflictError):
            await svc._resolveTool(dbSession, "ghost_tool")

    async def test_warmUp_idempotent(self, dbSession) -> None:
        """warmUp 多次调用无副作用。"""
        count1 = len(agent_tool_config_registry.all())
        await agent_tool_config_registry.warmUp(dbSession)
        count2 = len(agent_tool_config_registry.all())
        assert count1 == count2
        assert count1 == 3  # 3 个 seed 工具

    async def test_invalidate_full_reload(self, dbSession) -> None:
        """invalidate（无 name）后所有工具从 DB 重读。"""
        # 改一个工具
        await dbSession.execute(
            update(AgentToolConfig)
            .where(AgentToolConfig.name == "supplier_360")
            .values(description="updated via test")
        )
        await dbSession.commit()
        # 全量 invalidate（不传 name）→ 下次 warmUp 从 DB 重读
        agent_tool_config_registry.invalidate()
        await agent_tool_config_registry.warmUp(dbSession)
        tool = agent_tool_config_registry.get("supplier_360")
        assert tool is not None
        assert tool.description == "updated via test"