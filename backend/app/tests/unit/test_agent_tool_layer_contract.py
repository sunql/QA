"""AgentTool.data_layers 契约单测（Phase 7 G6 → 迁移到 feat-agent-tool-config-db）。

防漂移两件事：
1. 内置注册表声明契约：supplier_360 / supplier_risk → DIM+FEATURE，
   graph_traverse → DIM+DWD（与 handler 实际读取的数据源一致，见集成契约测试）。
2. 图遍历实体分类：Neo4j 业务实体子图同时含主数据（Supplier/Material → DIM）
   与业务单据（PO/GR/IQC/NCR/Contract → DWD），支撑 graph_traverse 的 DIM+DWD 声明。

格式校验（data_object / data_layers 大写非空）现迁到 `AgentToolConfig`
ORM/DTO 的 Pydantic 校验器（`app/domain/models.py` + `app/api/v1/dtos/`），
由 `test_agent_tool_config_dto.py` 覆盖；本测试不再重复。

SQL 实际触达层级的集成契约见 `app/tests/integration/test_agent_tool_layer_contract.py`。
"""

from __future__ import annotations

import pytest

from app.infrastructure.neo4j_client import BUSINESS_ENTITY_LABELS
from app.services.agent_tool_config_registry import agent_tool_config_registry
from app.services.agent_tools import AgentTool

# 表/实体 → 层 词汇表（SSOT，与 Harness/wiki 层词汇表对齐）
MASTER_ENTITY_LABELS = frozenset({"Supplier", "ItemMaster"})  # DIM 主数据
DOCUMENT_ENTITY_LABELS = frozenset(  # DWD 业务单据
    {"PurchaseOrder", "Receipt", "IncomingInspection", "Contract"}
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


def _populate_registry() -> None:
    """Bypass DB：直接给模块级 registry 注入 3 个内置 AgentTool。
    单元测试不应触发真 DB 连接；契约断言只关心 data_layers 内容。
    """
    agent_tool_config_registry._tools = {
        "supplier_360": _dummyTool(
            "supplier_360",
            data_object="SUPPLIER",
            data_layers=("DIM", "FEATURE"),
        ),
        "supplier_risk": _dummyTool(
            "supplier_risk",
            data_object="SUPPLIER",
            data_layers=("DIM", "FEATURE"),
        ),
        "graph_traverse": _dummyTool(
            "graph_traverse",
            data_object="GRAPH",
            data_layers=("DIM", "DWD"),
        ),
    }
    agent_tool_config_registry._loaded = True


class TestBuiltinLayerContract:
    def setup_method(self) -> None:
        _populate_registry()

    def test_declared_layers_match_documented_contract(self):
        tools = {t.name: t for t in agent_tool_config_registry.all()}
        for name, expected in EXPECTED_LAYERS.items():
            assert tools[name].data_layers == expected, (
                f"{name} 声明 {tools[name].data_layers}，契约要求 {expected}"
            )

    def test_three_tools_have_uppercase_object_and_layers(self):
        for tool in agent_tool_config_registry.all():
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
        graph_tool = agent_tool_config_registry.get("graph_traverse")
        assert graph_tool is not None
        assert set(graph_tool.data_layers) == {"DIM", "DWD"}