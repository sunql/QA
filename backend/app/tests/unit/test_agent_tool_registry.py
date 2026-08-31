"""Phase 6.4 Agent Tool 注册表单测。

覆盖：
- 注册 / 查询 / 列表 / 重复注册冲突 / 未知工具
- 内置注册表含 3 个工具，data_object 对齐 AgentAccessPolicy（SUPPLIER）
- 每个内置工具的 arg_extractor 能从中文问句解析出 key

handler 的真实执行在 test_agent_runtime_service.py（注入 fake handler）+ 集成测试验证。
"""

from __future__ import annotations

import pytest

from app.services.agent_tools import (
    AgentTool,
    AgentToolRegistry,
    agent_tool_registry,
)


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
    def test_has_three_tools_all_supplier(self):
        tools = {t.name: t for t in agent_tool_registry.list()}
        assert set(tools) == {"supplier_360", "supplier_risk", "graph_traverse"}
        for tool in tools.values():
            assert tool.data_object == "SUPPLIER"
            assert tool.description
            assert "key" in tool.input_schema["required"]

    def test_arg_extractors_parse_supplier_key(self):
        """内置 extractor 与 intent_service 正则对齐，中文问句可解析出 key。"""
        cases = {
            "supplier_360": "供应商 100001 的 360° 视图",
            "supplier_risk": "供应商 100001 的风险",
            "graph_traverse": "供应商 100001 涉及哪些物料",
        }
        for name, question in cases.items():
            tool = agent_tool_registry.get(name)
            assert tool is not None
            args = tool.arg_extractor(question)
            assert args == {"key": "100001"}, f"{name}: {args}"

    def test_arg_extractor_returns_none_without_key(self):
        tool = agent_tool_registry.get("supplier_risk")
        assert tool is not None
        assert tool.arg_extractor("今天天气怎么样") is None

    def test_arg_extractor_falls_back_to_generic_supplier_key(self):
        """显式指名 Agent 时，专用关键词（风险/涉及）缺失 → 回退通用 supplier key。

        「用 supplier_risk_agent 评估供应商 100001」无「风险」字样，
        但 Agent 已被指名，工具确定——回退 extractSupplierKey 即可执行。
        """
        cases = {
            "supplier_risk": "用 supplier_risk_agent 评估供应商 100001",
            "graph_traverse": "用 graph_reasoning_agent 分析供应商 100001",
        }
        for name, question in cases.items():
            tool = agent_tool_registry.get(name)
            assert tool is not None
            args = tool.arg_extractor(question)
            assert args == {"key": "100001"}, f"{name}: {args}"
