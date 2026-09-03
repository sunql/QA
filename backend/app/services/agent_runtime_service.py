"""Agent 运行时（Phase 6.4 feat-agent-runtime-mvp）。

``run(session, agent_code, input_text)`` 完整链路：

1. 注册解析：AgentRegistryService.getAgent → 不存在 NotFoundError(404)
2. 状态门禁：仅 ACTIVE 可运行 → ConflictError(409)
3. 工具解析：cache 绑定（无绑定的元数据 Agent → 409 不可运行）
4. 策略拦截（deny-by-default）：Agent 对 tool.data_object + tool.data_layers
   （每层）需有 READ / MASKED_READ 策略（data_layer=None 通配覆盖任意层）；
   FORBIDDEN / FORBIDDEN_WRITE 为显式否决，优先于任何 READ 授予。否则
   PermissionDeniedError(403)
5. 参数抽取：tool.arg_extractor(input_text) → None → ValidationError(422)
6. 执行 tool.handler → AgentRunRead（含 Token / 成本计量透传）

失败语义由全局 DomainError handler 映射（404 / 409 / 403 / 422），
chat 拦截路径捕获后转友好 answer（见 ChatService._handleAgentRun）。

依赖注入：registry（默认内置 agent_tool_registry）、agentService（默认
AgentRegistryService），便于单测替换为 fake。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AgentPermission, AgentStatus
from app.domain.error_messages import (
    MSG_AGENT_NOT_RUNNABLE,
    MSG_AGENT_NOT_RUNNABLE_NO_TOOL,
    MSG_AGENT_RUN_BAD_INPUT,
    MSG_AGENT_RUN_DENIED,
    MSG_AGENT_RUN_FORBIDDEN,
)
from app.domain.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.domain.models import AgentDefinition
from app.domain.schemas import AgentRunRead, _normalizeDataLayer
from app.infrastructure.llm.base_client import BaseLlmClient
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_tools import (
    AgentTool,
    AgentToolContext,
    AgentToolRegistry,
)
from app.services.agent_tool_config_registry import (
    AgentToolConfigRegistry,
    agent_tool_config_registry,
)
from app.services.agent_binding_cache import agent_binding_cache
from app.services.supplier_name_resolver import SupplierNameResolver

logger = logging.getLogger(__name__)

LlmFactory = Callable[[object], BaseLlmClient]

# 策略 permission 分组（_enforcePolicies 判定用）
_DENY = (
    AgentPermission.FORBIDDEN.value,
    AgentPermission.FORBIDDEN_WRITE.value,
)
_READABLE = (
    AgentPermission.READ.value,
    AgentPermission.MASKED_READ.value,
)


class AgentRuntimeService:
    """轻量 Agent 调度器：注册 → 工具路由 → 策略拦截 → 执行。"""

    def __init__(
        self,
        *,
        registry: AgentToolRegistry | None = None,
        agentService: AgentRegistryService | None = None,
        resolver: SupplierNameResolver | None = None,  # Phase 6.5：名字→编码预解析
        bindingCache=None,  # 测试注入 fake；运行时默认使用模块级单例
    ) -> None:
        self._registry: AgentToolConfigRegistry = registry or agent_tool_config_registry
        self._agents = agentService or AgentRegistryService()
        self._resolver = resolver or SupplierNameResolver()
        if bindingCache is not None:
            self._cache = bindingCache
        # else 使用 run() 中从 module-level singleton 读取（向后兼容）

    async def run(
        self,
        session: AsyncSession,
        agent_code: str,
        input_text: str,
        *,
        llm_factory: LlmFactory | None = None,
        actor: str = "runtime",
    ) -> AgentRunRead:
        """执行一次 Agent 运行（见模块 docstring 完整链路）。"""
        entity = await self._agents.getAgent(session, agent_code)  # NotFoundError(404)

        # DB cache（唯一数据源）：cache miss → 409（无 dict fallback）。
        # getToolName 在未 warmUp 时抛 RuntimeError（如测试环境 lifespan 未触发）；
        # 转为 None 以触发 409（而非 500）。
        cache = getattr(self, '_cache', None) or agent_binding_cache
        try:
            tool_name = cache.getToolName(agent_code)
        except RuntimeError:
            tool_name = None
        if tool_name is None:
            raise ConflictError(
                MSG_AGENT_NOT_RUNNABLE_NO_TOOL.format(code=agent_code)
            )

        if entity.status != AgentStatus.ACTIVE.value:
            status_label = entity.status or "unknown"
            raise ConflictError(
                MSG_AGENT_NOT_RUNNABLE.format(
                    code=agent_code, status=status_label
                )
            )

        tool = self._resolveTool(tool_name)
        self._enforcePolicies(entity, tool)

        # Phase 6.5：供应商名→编码预解析（数字未命中时查 entity_mapping.name；
        # 失败 raise ValidationError → 全局 handler 422 + details.candidates）
        resolved = await self._resolver.resolve(input_text, session)
        input_text = self._resolver.apply(input_text, resolved)

        args = tool.arg_extractor(input_text)
        if args is None:
            raise ValidationError(
                MSG_AGENT_RUN_BAD_INPUT.format(code=agent_code, tool=tool.name)
            )

        ctx = AgentToolContext(llm_factory=llm_factory, actor=actor)
        result = await tool.handler(session, args, ctx)
        return AgentRunRead(
            agent_code=entity.agent_code,
            agent_name=entity.agent_name,
            agent_owner=entity.owner,
            tool=tool.name,
            result=result.data,
            answer=result.answer,
            tokens_used=result.tokens_used,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost=result.cost,
            llm_model_name=result.llm_model_name,
            executed_at=datetime.now(timezone.utc),
        )

    def _resolveTool(self, name: str) -> AgentTool:
        tool = self._registry.get(name)
        if tool is None:
            raise ConflictError(
                MSG_AGENT_NOT_RUNNABLE.format(code=name, status="tool_unbound")
            )
        return tool

    def _enforcePolicies(self, entity: AgentDefinition, tool: AgentTool) -> None:
        """deny-by-default：工具读取的每个数据层都需被授权。

        - FORBIDDEN / FORBIDDEN_WRITE 为显式否决：对象匹配且层匹配（或 None 通配）
          即拒绝，优先级高于任何 READ 授予——防止跨层通配 READ 覆盖层级 FORBIDDEN。
        - 可读 = permission ∈ {READ, MASKED_READ}，且 data_layer 精确匹配或 None 通配；
          比较侧经 _normalizeDataLayer 归一化（strip+upper），历史非大写行同样生效。
        - 工具声明 data_layers：每层须可读；缺失任一层或任一层被显式禁止 → 403。
        - 工具不声明层（data_layers=()）→ 回退到 data_object 粒度（旧行为兼容），
          对象上任一 FORBIDDEN 同样优先于 READ。
        - MASKED_READ 视为可访问（敏感字段脱敏留待后续 Phase）。
        """
        if not tool.data_layers:
            # 层无关工具：对象粒度；显式否决优先于授予
            matching = [p for p in entity.policies if p.data_object == tool.data_object]
            if any(p.permission in _DENY for p in matching):
                raise PermissionDeniedError(
                    MSG_AGENT_RUN_FORBIDDEN.format(
                        code=entity.agent_code, object=tool.data_object, layer="任意"
                    )
                )
            if not any(p.permission in _READABLE for p in matching):
                raise PermissionDeniedError(
                    MSG_AGENT_RUN_DENIED.format(
                        code=entity.agent_code, object=tool.data_object, layer="任意"
                    )
                )
            return
        for layer in tool.data_layers:
            # 比较侧归一化（与写入边界 _normalizeDataLayer 一致）：历史/直改 DB 写入的
            # 非大写 data_layer（如 "feature"）也必须被识别，防 FORBIDDEN 漏判 fail-open。
            matching = [
                p
                for p in entity.policies
                if p.data_object == tool.data_object
                and _normalizeDataLayer(p.data_layer) in (None, layer)
            ]
            if any(p.permission in _DENY for p in matching):
                raise PermissionDeniedError(
                    MSG_AGENT_RUN_FORBIDDEN.format(
                        code=entity.agent_code, object=tool.data_object, layer=layer
                    )
                )
            if not any(p.permission in _READABLE for p in matching):
                raise PermissionDeniedError(
                    MSG_AGENT_RUN_DENIED.format(
                        code=entity.agent_code, object=tool.data_object, layer=layer
                    )
                )
