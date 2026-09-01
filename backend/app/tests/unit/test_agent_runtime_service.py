"""Phase 6.4 Agent 运行时服务单测。

注入 fake agentService（返回预设 AgentDefinition）+ fake registry（fake tool handler），
验证 run() 完整链路的编排与异常映射（不触数据库 / Neo4j）：

- 成功：3 个绑定工具（360 / risk / graph）→ AgentRunRead 全字段
- Token / 成本 / 模型名透传（来自 ToolResult）
- 404：Agent 不存在
- 409：DRAFT / DEPRECATED / 无工具绑定 / tool 未注册
- 403：FORBIDDEN 策略 / 无策略（deny-by-default）
- 通过：READ / MASKED_READ 策略
- 422：arg_extractor 解析失败（缺 key）

真实 handler（Supplier360Service / SupplierRiskService / GraphTraversalService）
在 test_agent_runtime_api.py + test_chat_agent_run.py 用真实 PG / Neo4j 验证。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.enums import AgentPermission, AgentStatus
from app.domain.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.domain.models import AgentAccessPolicy, AgentDefinition
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_runtime_service import AGENT_TOOLS, AgentRuntimeService
from app.services.agent_tools import (
    AgentTool,
    AgentToolRegistry,
    ToolResult,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fakeTool(
    name: str,
    *,
    data_object: str = "SUPPLIER",
    data_layers: tuple[str, ...] = (),
    extractor=lambda raw: {"key": "100001"},
) -> AgentTool:
    async def handler(session, args, ctx):  # noqa: ARG001
        return ToolResult(
            data={"handled": True, "key": args["key"]},
            answer=f"{name} executed",
            tokens_used=7,
            cost=0.014,
            llm_model_name="fake-llm",
        )

    return AgentTool(
        name=name,
        description="fake",
        data_object=data_object,
        data_layers=data_layers,
        input_schema={},
        arg_extractor=extractor,
        handler=handler,
    )


def _agent(
    code: str = "SUPPLIER_RISK_AGENT",
    *,
    status: str = AgentStatus.ACTIVE.value,
    policies: list[AgentAccessPolicy] | None = None,
    owner: str | None = "采购部",
) -> AgentDefinition:
    entity = AgentDefinition(
        agent_code=code,
        agent_name=f"{code} 名称",
        description="单测",
        trigger_type="user_question",
        response_latency="realtime",
        data_domains=["PROCUREMENT"],
        data_layers=["FEATURE"],
        status=status,
        owner=owner,
        version="v1.0",
    )
    entity.policies = list(
        policies
        if policies is not None
        else [
            AgentAccessPolicy(
                data_object="SUPPLIER",
                permission=AgentPermission.READ.value,
                data_layer=None,
                notes=None,
            )
        ]
    )
    return entity


def _readPolicy():
    return AgentAccessPolicy(
        data_object="SUPPLIER",
        permission=AgentPermission.READ.value,
        data_layer=None,
        notes=None,
    )


class _FakeAgentService:
    def __init__(self, entity: AgentDefinition | None = None):
        self._entity = entity

    async def getAgent(self, session, agent_code):  # noqa: ARG002
        if self._entity is not None and self._entity.agent_code == agent_code:
            return self._entity
        raise NotFoundError(f"Agent 不存在：code={agent_code}")


def _runtime(
    *,
    entity: AgentDefinition | None = None,
    tools: dict[str, AgentTool] | None = None,
) -> tuple[AgentRuntimeService, AgentToolRegistry]:
    registry = AgentToolRegistry()
    for tool in (tools or {}).values():
        registry.register(tool)
    service = AgentRuntimeService(
        registry=registry, agentService=_FakeAgentService(entity)
    )
    return service, registry


class TestRunSuccess:
    def test_run_supplier_risk_agent_success(self):
        entity = _agent("SUPPLIER_RISK_AGENT")
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk")},
        )
        run = _run(
            service.run(
                session=object(), agent_code="SUPPLIER_RISK_AGENT", input_text="评估供应商 100001"
            )
        )
        assert run.agent_code == "SUPPLIER_RISK_AGENT"
        assert run.tool == "supplier_risk"
        assert run.answer == "supplier_risk executed"
        assert run.result == {"handled": True, "key": "100001"}
        assert run.tokens_used == 7
        assert run.cost == 0.014
        assert run.llm_model_name == "fake-llm"
        assert run.executed_at is not None

    def test_run_supplier_360_agent_success(self):
        entity = _agent("SUPPLIER_360_AGENT")
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_360": _fakeTool("supplier_360")},
        )
        run = _run(
            service.run(session=object(), agent_code="SUPPLIER_360_AGENT", input_text="供应商 100001 的 360°")
        )
        assert run.tool == "supplier_360"
        assert run.agent_owner == "采购部"

    def test_run_graph_reasoning_agent_success(self):
        entity = _agent("GRAPH_REASONING_AGENT")
        service, _ = _runtime(
            entity=entity,
            tools={"graph_traverse": _fakeTool("graph_traverse")},
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="GRAPH_REASONING_AGENT",
                input_text="供应商 100001 涉及哪些物料",
            )
        )
        assert run.tool == "graph_traverse"

    def test_llm_factory_passed_to_context(self):
        """run 的 llm_factory 注入 AgentToolContext，透传给 tool handler。"""
        captured: dict[str, Any] = {}

        async def handler(session, args, ctx):  # noqa: ARG001
            captured["factory"] = ctx.llm_factory
            captured["actor"] = ctx.actor
            return ToolResult(data={"ok": True}, answer="ok")

        registry = AgentToolRegistry()
        registry.register(
            AgentTool(
                name="supplier_risk",
                description="fake",
                data_object="SUPPLIER",
                input_schema={},
                arg_extractor=lambda raw: {"key": "100001"},
                handler=handler,
            )
        )
        service = AgentRuntimeService(
            registry=registry, agentService=_FakeAgentService(_agent())
        )
        sentinel = object()
        _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
                llm_factory=sentinel,  # type: ignore[arg-type]
                actor="tester",
            )
        )
        assert captured["factory"] is sentinel
        assert captured["actor"] == "tester"


class TestRunFailures:
    def test_agent_not_found_raises_404(self):
        service, _ = _runtime(entity=None, tools={"supplier_risk": _fakeTool("supplier_risk")})
        with pytest.raises(NotFoundError):
            _run(service.run(session=object(), agent_code="UNKNOWN", input_text="x"))

    @pytest.mark.parametrize(
        "status",
        [AgentStatus.DRAFT.value, AgentStatus.DEPRECATED.value],
    )
    def test_non_active_status_raises_conflict(self, status: str):
        service, _ = _runtime(
            entity=_agent(status=status),
            tools={"supplier_risk": _fakeTool("supplier_risk")},
        )
        with pytest.raises(ConflictError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_agent_without_tool_binding_raises_conflict(self):
        """已注册但不在 AGENT_TOOLS 的元数据 Agent（如 SCHEDULED）→ 409。"""
        entity = _agent("PROCUREMENT_COPILOT_AGENT")
        assert "PROCUREMENT_COPILOT_AGENT" not in AGENT_TOOLS
        service, _ = _runtime(entity=entity, tools={})
        with pytest.raises(ConflictError, match="不可运行"):
            _run(
                service.run(
                    session=object(),
                    agent_code="PROCUREMENT_COPILOT_AGENT",
                    input_text="随便",
                )
            )

    def test_forbidden_policy_raises_permission_denied(self):
        entity = _agent(
            policies=[
                AgentAccessPolicy(
                    data_object="SUPPLIER",
                    permission=AgentPermission.FORBIDDEN.value,
                )
            ]
        )
        service, _ = _runtime(
            entity=entity, tools={"supplier_risk": _fakeTool("supplier_risk")}
        )
        with pytest.raises(PermissionDeniedError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_missing_policy_denies_by_default(self):
        """deny-by-default：Agent 对 tool.data_object 无任何策略 → 403。"""
        entity = _agent(policies=[])
        service, _ = _runtime(
            entity=entity, tools={"supplier_risk": _fakeTool("supplier_risk")}
        )
        with pytest.raises(PermissionDeniedError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_policy_for_different_data_object_denies(self):
        """Agent 有策略但对象不是 tool.data_object → 403。"""
        entity = _agent(
            policies=[
                AgentAccessPolicy(
                    data_object="PURCHASE_ORDER",
                    permission=AgentPermission.READ.value,
                )
            ]
        )
        service, _ = _runtime(
            entity=entity, tools={"supplier_risk": _fakeTool("supplier_risk")}
        )
        with pytest.raises(PermissionDeniedError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_bad_input_raises_validation_error(self):
        """arg_extractor 解析失败（缺 supplier key）→ 422，handler 不被调用。"""
        called: list[bool] = []

        async def handler(session, args, ctx):  # noqa: ARG001
            called.append(True)
            return ToolResult(data={}, answer="never")

        registry = AgentToolRegistry()
        registry.register(
            AgentTool(
                name="supplier_risk",
                description="fake",
                data_object="SUPPLIER",
                input_schema={},
                arg_extractor=lambda raw: None,
                handler=handler,
            )
        )
        service = AgentRuntimeService(
            registry=registry, agentService=_FakeAgentService(_agent())
        )
        with pytest.raises(ValidationError, match="解析执行参数"):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="今天天气怎么样",
                )
            )
        assert called == []


class TestPolicyAllow:
    def test_read_policy_allows(self):
        service, _ = _runtime(
            entity=_agent(),
            tools={"supplier_risk": _fakeTool("supplier_risk")},
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
            )
        )
        assert run.tool == "supplier_risk"

    def test_masked_read_policy_allows(self):
        """MASKED_READ 视为可访问（脱敏留待后续 Phase）。"""
        entity = _agent(
            policies=[
                AgentAccessPolicy(
                    data_object="SUPPLIER",
                    permission=AgentPermission.MASKED_READ.value,
                )
            ]
        )
        service, _ = _runtime(
            entity=entity, tools={"supplier_risk": _fakeTool("supplier_risk")}
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
            )
        )
        assert run.tool == "supplier_risk"


def _layerPolicy(
    *,
    data_layer: str | None = None,
    permission: str = AgentPermission.READ.value,
) -> AgentAccessPolicy:
    """构造单条 data_layer 策略（分层粒度测试用）。"""
    return AgentAccessPolicy(
        data_object="SUPPLIER",
        permission=permission,
        data_layer=data_layer,
        notes=None,
    )


class TestLayerPolicy:
    """data_layer 策略粒度（Phase 7+ 安全补强）。

    工具声明读取的数据层（data_layers）；运行时要求每层都被授权
    （策略 data_layer 精确匹配或 None 通配），否则 PermissionDeniedError(403)。
    """

    def test_layer_exact_match_allows(self):
        """FEATURE 工具 + FEATURE 策略 → 通过。"""
        entity = _agent(
            policies=[
                _layerPolicy(data_layer="FEATURE"),
                _layerPolicy(data_layer="DIM"),
            ]
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
            )
        )
        assert run.tool == "supplier_risk"

    def test_layer_none_wildcard_covers_any_layer(self):
        """data_layer=None 通配策略 → 覆盖工具声明的任意层。"""
        entity = _agent(
            policies=[_layerPolicy(data_layer=None)]  # 跨层通用
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE", "DIM"))},
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
            )
        )
        assert run.tool == "supplier_risk"

    def test_layer_mismatch_denies(self):
        """FEATURE 工具 + 仅 DIM 策略 → 403（层粒度生效）。"""
        entity = _agent(
            policies=[_layerPolicy(data_layer="DIM")],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        with pytest.raises(PermissionDeniedError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_multi_layer_missing_one_denies(self):
        """(DIM, FEATURE) 工具 + 仅 DIM 策略 → 403（缺失 FEATURE）。"""
        entity = _agent(
            policies=[_layerPolicy(data_layer="DIM")],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("DIM", "FEATURE"))},
        )
        with pytest.raises(PermissionDeniedError, match="FEATURE"):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_layerless_tool_falls_back_to_object_only(self):
        """工具不声明层（data_layers=()）→ 回退到 data_object 粒度（旧行为兼容）。"""
        entity = _agent(
            policies=[_layerPolicy(data_layer=None)],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk")},  # data_layers=()
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
            )
        )
        assert run.tool == "supplier_risk"

    def test_layer_forbidden_denies_even_with_matching_layer(self):
        """FORBIDDEN 策略（即使层精确匹配）→ 403。"""
        entity = _agent(
            policies=[
                _layerPolicy(data_layer="FEATURE", permission=AgentPermission.FORBIDDEN.value),
            ],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        with pytest.raises(PermissionDeniedError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_layer_forbidden_overrides_wildcard_read(self):
        """HIGH 缺口回归：通配 None READ 不能覆盖层级 FORBIDDEN（显式否决优先）。

        管理员先给 SUPPLIER@None=READ 跨层便利授权，再对 FEATURE 层设 FORBIDDEN
        （隔离敏感特征数据）——工具读 FEATURE 必须被拒绝。
        """
        entity = _agent(
            policies=[
                _layerPolicy(data_layer=None),  # 跨层通用 READ（旧种子遗留）
                _layerPolicy(
                    data_layer="FEATURE", permission=AgentPermission.FORBIDDEN.value
                ),
            ],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        with pytest.raises(PermissionDeniedError):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_layer_forbidden_message_is_distinct(self):
        """显式否决用独立消息（含 FORBIDDEN），区别于「无读取策略」。"""
        entity = _agent(
            policies=[
                _layerPolicy(data_layer=None),
                _layerPolicy(
                    data_layer="FEATURE", permission=AgentPermission.FORBIDDEN.value
                ),
            ],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        with pytest.raises(PermissionDeniedError, match="FORBIDDEN"):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_layerless_tool_forbidden_overrides_wildcard_read(self):
        """层无关工具 + 通配 READ + 对象上任一层 FORBIDDEN → 403（对象粒度否决优先）。"""
        entity = _agent(
            policies=[
                _layerPolicy(data_layer=None),
                _layerPolicy(
                    data_layer="FEATURE", permission=AgentPermission.FORBIDDEN.value
                ),
            ],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk")},  # data_layers=()
        )
        with pytest.raises(PermissionDeniedError, match="FORBIDDEN"):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_forbidden_normalizes_non_canonical_layer_at_read_time(self):
        """MEDIUM 缺口回归：历史非大写 data_layer（如 "feature"）在比较侧被归一化。

        写入边界归一化只保护新数据；直改 DB / 旧 API 写入的小写 FORBIDDEN 若不被识别，
        会被通配 READ 覆盖（fail-open）——正是本 change 要封堵的可绕过配置。
        """
        entity = _agent(
            policies=[
                _layerPolicy(data_layer=None),  # 通配 READ
                _layerPolicy(
                    data_layer="feature",  # 非规范大小写（历史/直改 DB 行）
                    permission=AgentPermission.FORBIDDEN.value,
                ),
            ],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        with pytest.raises(PermissionDeniedError, match="FORBIDDEN"):
            _run(
                service.run(
                    session=object(),
                    agent_code="SUPPLIER_RISK_AGENT",
                    input_text="评估供应商 100001",
                )
            )

    def test_grant_normalizes_non_canonical_layer_at_read_time(self):
        """非大写 data_layer 的 READ 授予同样在比较侧生效（fail-closed 侧对称修复）。"""
        entity = _agent(
            policies=[_layerPolicy(data_layer=" feature ")],
        )
        service, _ = _runtime(
            entity=entity,
            tools={"supplier_risk": _fakeTool("supplier_risk", data_layers=("FEATURE",))},
        )
        run = _run(
            service.run(
                session=object(),
                agent_code="SUPPLIER_RISK_AGENT",
                input_text="评估供应商 100001",
            )
        )
        assert run.tool == "supplier_risk"
