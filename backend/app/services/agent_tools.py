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
    prompt_tokens: int = 0
    completion_tokens: int = 0
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
        self._validate(tool)
        self._tools[tool.name] = tool

    @staticmethod
    def _validate(tool: AgentTool) -> None:
        """注册期防漂移校验（Phase 7 G6）：data_object / data_layers 全大写。

        运行时 `_enforcePolicies` 用它们与 `AgentAccessPolicy.data_layer` 做精确
        比较（策略侧已 `_normalizeDataLayer` 归一化大写），工具侧若大小写不一致，
        授权会静默 fail-closed（403）或漏判 —— 这里在注册时尽早报错，阻止坏注册。
        层无关工具允许 `data_layers=()`（回退对象粒度，旧行为兼容）。
        """
        if not tool.data_object or tool.data_object != tool.data_object.strip().upper():
            raise ValueError(
                f"tool {tool.name}: data_object 必须非空全大写，got {tool.data_object!r}"
            )
        for layer in tool.data_layers:
            if not layer or layer != layer.strip().upper():
                raise ValueError(
                    f"tool {tool.name}: data_layers 元素必须非空全大写，got {layer!r}"
                )

    def validate(self) -> None:
        """全量自检（构建完成后兜底；register 已逐条校验）。"""
        for tool in self._tools.values():
            self._validate(tool)

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self) -> tuple[AgentTool, ...]:
        return tuple(self._tools.values())

    def all(self) -> list[AgentTool]:
        """所有已注册工具，按 name 排序。

        Phase 7 feat-agent-tool-binding Task 4：写时 Pydantic 校验需枚举已注册
        工具名以生成错误消息；list() 返回 tuple 不可变，调用方按名字排序更便于
        拼接注册表展示。
        """
        return [self._tools[k] for k in sorted(self._tools.keys())]


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
    # Phase 6.x：透传字符串 supplier_code（THBI '10105' 或合成 'SUP000001'），
    # service 内 _resolveSupplier 同时支持 enterprise_code/enterprise_key 双路查询。
    # 不再做 int() 强制转换（THBI 业务码 '10105' int() 后变 10105，无法命中 hash）。
    key = args["key"]
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
    # Phase 6.x：透传字符串 supplier_code（详见 _supplier360Handler 注释）。
    key = args["key"]
    read = await SupplierRiskService().assess(
        session, key, llm_factory=ctx.llm_factory
    )
    return ToolResult(
        data=read.model_dump(mode="json", by_alias=True),
        answer=buildRiskAnswer(read, key),
        tokens_used=read.tokens_used,
        prompt_tokens=read.prompt_tokens,
        completion_tokens=read.completion_tokens,
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
                    "key": {
                        "type": "string",
                        "description": (
                            "供应商企业编码（THBI '10105' 或合成 SUP000001）或"
                            " BIGINT enterprise_key 代理键（service 自动双路解析）"
                        ),
                    }
                },
                "required": ["key"],
            },
            # 与 risk/graph 一致走 _supplierKeyArgs：显式指名 Agent（Runtime 页）时
            # 360 意图关键词是冗余的（如「查询供应商 济南吉利汽车有限公司 的情况」，
            # 名称已被 resolver 预解析成编码），回退通用 supplier key
            arg_extractor=lambda raw: _supplierKeyArgs(raw, extractSupplierKey),
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
                    "key": {
                        "type": "string",
                        "description": (
                            "供应商企业编码（THBI '10105' 或合成 SUP000001）或"
                            " BIGINT enterprise_key 代理键（service 自动双路解析）"
                        ),
                    }
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
                    "key": {
                        "type": "string",
                        "description": (
                            "供应商企业编码或 BIGINT enterprise_key 代理键"
                            "（service 自动双路解析）"
                        ),
                    }
                },
                "required": ["key"],
            },
            arg_extractor=lambda raw: _supplierKeyArgs(raw, extractSupplierGraphKey),
            handler=_graphTraverseHandler,
        )
    )
    registry.validate()  # 构建完成自检：内置工具数据层声明必须合规（G6）
    return registry


# 模块级单例：运行时与测试共享；测试可注入自定义 registry 覆盖内置工具。
agent_tool_registry = _buildRegistry()


# Agent → tool 默认绑定常量（仅作 seed 数据源；运行时 cache 为唯一数据源）。
AGENT_DEFAULT_BINDINGS: dict[str, str] = {
    "SUPPLIER_360_AGENT": "supplier_360",
    "SUPPLIER_RISK_AGENT": "supplier_risk",
    "GRAPH_REASONING_AGENT": "graph_traverse",
}


# ---------------------------------------------------------------------------
# feat-agent-tool-config-db (2026-09-03)：handler dicts + AgentToolAssembly
# ---------------------------------------------------------------------------


async def _nl2sqlDefaultHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """NL2SQL handler：业务人员配的自然语言查询经 NL2SqlService 翻译执行。

    v1 仅暴露入口；完整 NL2SQL 流水线（ontology class 收集 + model 解析 + SQL
    Guard）由后续 task 接入。当前返回最小 ToolResult，便于 admin 在 UI 配置
    handler_kind=NL2SQL 的工具且 registry 装配成功；运行时若真正触发会清晰报错。
    """
    question = args.get("question", "")
    logger.info("nl2sql_default handler invoked: question_len=%d actor=%s", len(question), ctx.actor)
    return ToolResult(
        data={"question": question, "status": "nl2sql_handler_pending_full_wiring"},
        answer=(
            "NL2SQL handler 框架已就位；完整流水线（本体 + LLM + SQL Guard）"
            "将在后续 task 接入。当前仅验证 registry 装配路径。"
        ),
        tokens_used=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost=0.0,
        llm_model_name=None,
    )


BUILTIN_HANDLERS: dict[str, AgentHandler] = {
    "supplier_360": _supplier360Handler,
    "supplier_risk": _supplierRiskHandler,
    "graph_traverse": _graphTraverseHandler,
}

NL2SQL_HANDLERS: dict[str, AgentHandler] = {
    "nl2sql_default": _nl2sqlDefaultHandler,
}

ARG_EXTRACTORS: dict[str, ArgExtractor] = {
    "supplier_key": lambda raw: _supplierKeyArgs(raw, extractSupplierKey),
    "supplier_risk_key": lambda raw: _supplierKeyArgs(raw, extractSupplierRiskKey),
    "supplier_graph_key": lambda raw: _supplierKeyArgs(raw, extractSupplierGraphKey),
}

_VALID_HANDLER_REFS: dict[str, frozenset[str]] = {
    "BUILTIN": frozenset(BUILTIN_HANDLERS.keys()),
    "NL2SQL": frozenset(NL2SQL_HANDLERS.keys()),
}


class AgentToolAssembly:
    """DB 行 → AgentTool 装配器（feat-agent-tool-config-db, 2026-09-03）。

    校验 handler_kind/ref/arg_extractor_kind，合并 DB 元数据 + 代码侧 handler。
    """

    @staticmethod
    def assemble(config_row) -> AgentTool:
        from app.domain.models import AgentToolConfig
        from app.domain.schemas import _normalizeDataObject

        kind = config_row.handler_kind
        ref = config_row.handler_ref
        ext_kind = config_row.arg_extractor_kind

        if kind not in _VALID_HANDLER_REFS:
            raise ValueError(
                f"agent_tool_config {config_row.name}: handler_kind 不支持: {kind!r}"
            )
        if ref not in _VALID_HANDLER_REFS[kind]:
            raise ValueError(
                f"agent_tool_config {config_row.name}: handler_ref {ref!r} 不在 {kind} 白名单"
            )
        if ext_kind not in ARG_EXTRACTORS:
            raise ValueError(
                f"agent_tool_config {config_row.name}: arg_extractor_kind {ext_kind!r} 不存在"
            )

        handlers: dict[str, AgentHandler] = {**BUILTIN_HANDLERS, **NL2SQL_HANDLERS}
        return AgentTool(
            name=config_row.name,
            description=config_row.description or "",
            data_object=_normalizeDataObject(config_row.data_object),
            data_layers=tuple(
                layer.strip().upper() for layer in (config_row.data_layers or [])
            ),
            input_schema=config_row.input_schema or {},
            arg_extractor=ARG_EXTRACTORS[ext_kind],
            handler=handlers[ref],
        )
