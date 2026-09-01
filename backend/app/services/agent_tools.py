"""Agent 可调用工具注册表（Phase 6.4 feat-agent-runtime-mvp）。

function-calling 风格注册：每个 ``AgentTool`` 声明 name / description / data_object /
input_schema / arg_extractor / handler。``AgentRuntimeService`` 按 Agent 绑定工具，
先做数据对象策略拦截（AgentAccessPolicy），再抽取参数、执行 handler。

MVP 注册 3 个工具，包装既有领域服务：

- ``supplier_360``   → Supplier360Service.get360（只读聚合，无 LLM）
- ``supplier_risk``  → SupplierRiskService.assess（透传 llm_factory + Token 计量）
- ``graph_traverse`` → GraphTraversalService.traverseForChat（Neo4j 多跳推理，无 LLM）

约定：
- handler 签名 ``(session, args, ctx)``，args 由 arg_extractor 从原始输入解析；
  解析失败（key 缺失）由 arg_extractor 返回 None，运行时抛 ValidationError(422)。
- 工具对数据对象的访问由 AgentAccessPolicy 管控（deny-by-default），见 runtime。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.llm.base_client import BaseLlmClient
from app.services.graph_traversal_service import GraphTraversalService
from app.services.intent_service import (
    extractSupplierAnyKey,
    extractSupplierGraphKey,
    extractSupplierKey,
    extractSupplierRiskKey,
)
from app.services.supplier_360_service import Supplier360Service
from app.services.supplier_risk_service import SupplierRiskService, buildRiskAnswer

logger = logging.getLogger(__name__)

LlmFactory = Callable[[Any], BaseLlmClient]


@dataclass(frozen=True)
class AgentToolContext:
    """工具执行上下文（每次 run 注入；不可变）。"""

    llm_factory: LlmFactory | None = None
    actor: str = "runtime"


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。data 必须 JSON-serializable（camelCase alias 对齐 API 契约）。"""

    data: dict
    answer: str
    tokens_used: int = 0
    cost: float = 0.0
    llm_model_name: str | None = None


AgentHandler = Callable[[AsyncSession, dict, AgentToolContext], Awaitable[ToolResult]]
ArgExtractor = Callable[[str], dict | None]


@dataclass(frozen=True)
class AgentTool:
    """Agent 可调用的工具（function-calling 风格注册元数据）。"""

    name: str
    description: str
    data_object: str  # ACL 主题，与 AgentAccessPolicy.data_object 对齐
    input_schema: dict  # JSON Schema（供未来 LLM function calling 复用）
    arg_extractor: ArgExtractor  # 原始输入 → args dict；解析失败返回 None
    handler: AgentHandler  # 用解析后的 args 执行并返回 ToolResult
    data_layers: tuple[str, ...] = ()  # 工具读取的数据层（DIM/DWD/FEATURE…）；() = 层无关（回退到对象粒度）


class AgentToolRegistry:
    """工具注册表：按 name 唯一注册，运行时按名解析。"""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> tuple[AgentTool, ...]:
        return tuple(self._tools.values())


# ---------------------------------------------------------------------------
# 3 个内置工具的 arg_extractor / handler
# ---------------------------------------------------------------------------


def _keyOrNone(key: str | None) -> dict | None:
    return {"key": key} if key is not None else None


def _supplierKeyArgs(raw: str, specialized: Callable[[str], str | None]) -> dict | None:
    """解析供应商 key 参数：专用 extractor 优先，失败回退通用 supplier key。

    当用户在 chat 中显式指名 Agent（如「用 supplier_risk_agent 评估供应商 100001」）
    时，工具已被确定，risk/graph 等意图关键词变成冗余——回退到通用供应商编码
    解析即可满足「Agent → Tool → 响应」MVP 验收（agent_tools.py docstring 对齐）。
    """
    return _keyOrNone(specialized(raw) or extractSupplierAnyKey(raw))


async def _supplier360Handler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    key = int(args["key"])
    view = await Supplier360Service().get360(session, key)
    return ToolResult(
        data=view.model_dump(mode="json", by_alias=True),
        answer=(
            f"供应商 {view.profile.enterprise_code}（{key}）360° 视图已生成："
            f"{len(view.kpis)} 项表现指标、{len(view.entity_codes)} 个跨系统编码。"
        ),
    )


async def _supplierRiskHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    key = int(args["key"])
    read = await SupplierRiskService().assess(
        session, key, llm_factory=ctx.llm_factory
    )
    return ToolResult(
        data=read.model_dump(mode="json", by_alias=True),
        answer=buildRiskAnswer(read, key),
        tokens_used=read.tokens_used,
        cost=read.cost,
        llm_model_name=read.llm_model_name,
    )


async def _graphTraverseHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    service = GraphTraversalService()
    traversal = await service.traverseForChat(str(args["key"]))
    return ToolResult(
        data=traversal.model_dump(mode="json", by_alias=True),
        answer=service.buildChatAnswer(traversal),
    )


def _buildRegistry() -> AgentToolRegistry:
    """构建内置工具注册表（3 个工具，均为 SUPPLIER 数据对象）。"""
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="supplier_360",
            description="查询单供应商 360° 视图（主数据 + 交付/质量/价格表现 + 跨系统编码）",
            data_object="SUPPLIER",
            # 读 EntityMapping（DIM 主数据）+ FeatureDefinition/FeatureValue（FEATURE 特征层）
            data_layers=("DIM", "FEATURE"),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "供应商企业编码"}
                },
                "required": ["key"],
            },
            arg_extractor=lambda raw: _keyOrNone(extractSupplierKey(raw)),
            handler=_supplier360Handler,
        )
    )
    registry.register(
        AgentTool(
            name="supplier_risk",
            description=(
                "评估单供应商风险等级（RISK_SCORE 主路径 + 其他 feature fallback + LLM 风险点）"
            ),
            data_object="SUPPLIER",
            # 经 Supplier360Service.get360 读 EntityMapping（DIM）+ Feature（FEATURE）
            data_layers=("DIM", "FEATURE"),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "供应商企业编码"}
                },
                "required": ["key"],
            },
            arg_extractor=lambda raw: _supplierKeyArgs(raw, extractSupplierRiskKey),
            handler=_supplierRiskHandler,
        )
    )
    registry.register(
        AgentTool(
            name="graph_traverse",
            description=(
                "供应链链路推理：从供应商出发的多跳可达业务实体（物料/订单/收货/问题）"
            ),
            data_object="SUPPLIER",
            # Neo4j 业务实体子图（DIM 主数据实体 + DWD 业务单据实体）
            data_layers=("DIM", "DWD"),
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "供应商企业编码"}
                },
                "required": ["key"],
            },
            arg_extractor=lambda raw: _supplierKeyArgs(raw, extractSupplierGraphKey),
            handler=_graphTraverseHandler,
        )
    )
    return registry


# 模块级单例：运行时与测试共享；测试可注入自定义 registry 覆盖内置工具。
agent_tool_registry = _buildRegistry()
