"""AgentTool.data_layers 契约单测（Phase 7 G6 feat-agent-tool-layer-contract）。

防漂移三件事：
1. 注册期格式校验：data_object / data_layers 元素必须非空全大写
   （运行时策略比较依赖大写，坏格式会导致静默 fail-closed 或漏判）。
2. 内置注册表声明契约：supplier_360 / supplier_risk → DIM+FEATURE，
   graph_traverse → DIM+DWD（与 handler 实际读取的数据源一致，见集成契约测试）。
3. 图遍历实体分类：Neo4j 业务实体子图同时含主数据（Supplier/Material → DIM）
   与业务单据（PO/GR/IQC/NCR/Contract → DWD），支撑 graph_traverse 的 DIM+DWD 声明。

SQL 实际触达层级的集成契约见 `app/tests/integration/test_agent_tool_layer_contract.py`。
"""

from __future__ import annotations

import pytest

from app.infrastructure.neo4j_client import BUSINESS_ENTITY_LABELS
from app.services.agent_tools import (
    AgentTool,
    AgentToolRegistry,
    agent_tool_registry,
)

# 表/实体 → 层 词汇表（SSOT，与 Harness/wiki 层词汇表对齐）
MASTER_ENTITY_LABELS = frozenset({"Supplier", "Material"})  # DIM 主数据
DOCUMENT_ENTITY_LABELS = frozenset(  # DWD 业务单据
    {"PurchaseOrder", "GoodsReceipt", "IncomingInspection", "NCR", "Contract"}
)

# 内置工具声明契约（SSOT：与 handler 实际读取的数据源一致）
EXPECTED_LAYERS: dict[str, tuple[str, ...]] = {
    "supplier_360": ("DIM", "FEATURE"),
    "supplier_risk": ("DIM", "FEATURE"),
    "graph_traverse": ("DIM", "DWD"),
}


def _dummyTool(
    name: str = "t1",
    data_object: str = "SUPPLIER",
    data_layers: tuple[str, ...] = (),
) -> AgentTool:
    async def handler(session, args, ctx):  # noqa: ARG001
        return None

    return AgentTool(
        name=name,
        description="dummy",
        data_object=data_object,
        data_layers=data_layers,
        input_schema={},
        arg_extractor=lambda raw: {"key": "1"},
        handler=handler,
    )


class TestRegisterFormatValidation:
    def test_rejects_lowercase_data_object(self):
        registry = AgentToolRegistry()
        with pytest.raises(ValueError, match="data_object 必须非空全大写"):
            registry.register(_dummyTool(data_object="supplier"))

    def test_rejects_mixed_case_data_object(self):
        registry = AgentToolRegistry()
        with pytest.raises(ValueError, match="data_object 必须非空全大写"):
            registry.register(_dummyTool(data_object="Supplier"))

    def test_rejects_empty_data_object(self):
        registry = AgentToolRegistry()
        with pytest.raises(ValueError, match="data_object 必须非空全大写"):
            registry.register(_dummyTool(data_object=""))

    def test_rejects_lowercase_layer(self):
        registry = AgentToolRegistry()
        with pytest.raises(ValueError, match="data_layers 元素必须非空全大写"):
            registry.register(_dummyTool(data_layers=("dim",)))

    def test_rejects_whitespace_padded_layer(self):
        registry = AgentToolRegistry()
        with pytest.raises(ValueError, match="data_layers 元素必须非空全大写"):
            registry.register(_dummyTool(data_layers=(" DIM",)))

    def test_rejects_empty_layer(self):
        registry = AgentToolRegistry()
        with pytest.raises(ValueError, match="data_layers 元素必须非空全大写"):
            registry.register(_dummyTool(data_layers=("",)))

    def test_accepts_uppercase_object_and_layers(self):
        registry = AgentToolRegistry()
        tool = _dummyTool(data_layers=("DIM", "FEATURE"))
        registry.register(tool)
        assert registry.get("t1") is tool

    def test_accepts_layer_agnostic_empty_layers(self):
        """data_layers=() 层无关工具合法（回退对象粒度，旧行为兼容）。"""
        registry = AgentToolRegistry()
        tool = _dummyTool(data_layers=())
        registry.register(tool)
        assert registry.get("t1") is tool

    def test_validate_full_registry(self):
        """全量自检：即使绕过 register 注入违规工具，validate 也兜底报错。"""
        registry = AgentToolRegistry()
        registry.register(_dummyTool("ok", data_layers=("DIM",)))
        # 白盒注入绕过 register 校验，验证 validate 兜底（防御外部直接改 _tools）
        registry._tools["bad"] = _dummyTool("bad", data_layers=("feature",))
        with pytest.raises(ValueError, match="data_layers 元素必须非空全大写"):
            registry.validate()


class TestBuiltinLayerContract:
    def test_declared_layers_match_documented_contract(self):
        tools = {t.name: t for t in agent_tool_registry.list()}
        for name, expected in EXPECTED_LAYERS.items():
            assert tools[name].data_layers == expected, (
                f"{name} 声明 {tools[name].data_layers}，契约要求 {expected}"
            )

    def test_three_tools_have_uppercase_object_and_layers(self):
        for tool in agent_tool_registry.list():
            assert tool.data_object == tool.data_object.strip().upper()
            for layer in tool.data_layers:
                assert layer == layer.strip().upper()

    def test_graph_entity_labels_classify_into_dim_and_dwd(self):
        """graph_traverse 的 DIM+DWD 声明由真实图实体分类支撑。"""
        assert MASTER_ENTITY_LABELS <= BUSINESS_ENTITY_LABELS  # DIM 主数据
        assert DOCUMENT_ENTITY_LABELS <= BUSINESS_ENTITY_LABELS  # DWD 业务单据
        # 图里不能有未分类的实体（分类字典需随图扩展，契约才不失真）
        assert MASTER_ENTITY_LABELS | DOCUMENT_ENTITY_LABELS == BUSINESS_ENTITY_LABELS
        # 工具层声明覆盖图实体两层
        graph_tool = agent_tool_registry.get("graph_traverse")
        assert graph_tool is not None
        assert set(graph_tool.data_layers) == {"DIM", "DWD"}
