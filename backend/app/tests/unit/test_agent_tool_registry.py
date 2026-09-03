"""Phase 6.4 Agent Tool 注册表单测 + T8 删除影响测试。

覆盖：
- 注册 / 查询 / 列表 / 重复注册冲突 / 未知工具（AgentToolRegistry dataclass 自身）
- T8：硬编码模块单例 _buildRegistry / agent_tool_registry / AGENT_DEFAULT_BINDINGS
  已删除；运行时数据源为 agent_tool_config_registry（lifespan warmUp）

handler 的真实执行在 test_agent_runtime_service.py（注入 fake handler）+ 集成测试验证。
"""

from __future__ import annotations

import pytest

from app.services.agent_tools import AgentTool, AgentToolRegistry


def _dummyTool(name: str = "t1", data_object: str = "X") -> AgentTool:
    async def handler(session, args, ctx):  # noqa: ARG001
        return None

    return AgentTool(
        name=name,
        description="dummy",
        data_object=data_object,
        input_schema={},
        arg_extractor=lambda raw: {"key": "1"},
        handler=handler,
    )


class TestAgentToolRegistry:
    def test_register_get_has_list(self):
        registry = AgentToolRegistry()
        tool = _dummyTool("t1")
        registry.register(tool)
        assert registry.get("t1") is tool
        assert registry.has("t1") is True
        assert registry.has("nope") is False
        assert registry.get("nope") is None
        assert tuple(t.name for t in registry.list()) == ("t1",)

    def test_duplicate_register_raises_value_error(self):
        registry = AgentToolRegistry()
        registry.register(_dummyTool("dup"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_dummyTool("dup"))


class TestBuiltinRegistry:
    def test_module_singletons_removed(self):
        """T8 (feat-agent-tool-config-db)：硬编码 _buildRegistry / agent_tool_registry /
        AGENT_DEFAULT_BINDINGS 已删除，运行时改读 agent_tool_config_registry（lifespan warmUp）。"""
        import app.services.agent_tools as mod
        assert not hasattr(mod, "_buildRegistry"), (
            "_buildRegistry 已迁至 DB（agent_tool_config），不允许残留"
        )
        assert not hasattr(mod, "agent_tool_registry"), (
            "agent_tool_registry 单例已迁至 agent_tool_config_registry"
        )
        assert not hasattr(mod, "AGENT_DEFAULT_BINDINGS"), (
            "AGENT_DEFAULT_BINDINGS 绑定常量已迁至 seed_agent_tool_configs（DB）"
        )

    def test_BUILTIN_HANDLERS_unchanged(self):
        """handler dicts 仍由代码持有（BUILTIN/NL2SQL），DB 仅存元数据。"""
        from app.services.agent_tools import BUILTIN_HANDLERS
        assert set(BUILTIN_HANDLERS.keys()) == {
            "supplier_360", "supplier_risk", "graph_traverse"
        }

    def test_ARG_EXTRACTORS_unchanged(self):
        from app.services.agent_tools import ARG_EXTRACTORS
        assert set(ARG_EXTRACTORS.keys()) == {
            "supplier_key", "supplier_risk_key", "supplier_graph_key",
        }

    def test_arg_extractors_parse_supplier_key(self):
        """内置 extractor 与 intent_service 正则对齐，中文问句可解析出 key。"""
        from app.services.agent_tools import ARG_EXTRACTORS
        cases = {
            "supplier_key": "供应商 100001 的情况",
            "supplier_risk_key": "供应商 100001 的风险",
            "supplier_graph_key": "供应商 100001 涉及哪些物料",
        }
        for kind, question in cases.items():
            extractor = ARG_EXTRACTORS[kind]
            args = extractor(question)
            assert args == {"key": "100001"}, f"{kind}: {args}"

    def test_arg_extractor_returns_none_without_key(self):
        from app.services.agent_tools import ARG_EXTRACTORS
        assert ARG_EXTRACTORS["supplier_risk_key"]("今天天气怎么样") is None
