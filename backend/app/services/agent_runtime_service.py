"""Agent 运行时（Phase 6.4 feat-agent-runtime-mvp）。

``run(session, agent_code, input_text)`` 完整链路：

1. 注册解析：AgentRegistryService.getAgent → 不存在 NotFoundError(404)
2. 状态门禁：仅 ACTIVE 可运行 → ConflictError(409)
3. 工具解析：AGENT_TOOLS 绑定（无绑定的元数据 Agent → 409 不可运行）
4. 策略拦截（deny-by-default）：Agent 对 tool.data_object 需有 READ / MASKED_READ
   策略，否则 PermissionDeniedError(403)
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
    MSG_AGENT_RUN_BAD_INPUT,
    MSG_AGENT_RUN_DENIED,
)
from app.domain.exceptions import ConflictError, PermissionDeniedError, ValidationError
from app.domain.models import AgentDefinition
from app.domain.schemas import AgentRunRead
from app.infrastructure.llm.base_client import BaseLlmClient
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_tools import (
    AgentTool,
    AgentToolContext,
    AgentToolRegistry,
    agent_tool_registry,
)

logger = logging.getLogger(__name__)

LlmFactory = Callable[[object], BaseLlmClient]

# agent_code → 可调用工具（顺序即优先级；MVP 每 Agent 绑定 1 个工具）。
# 不在映射中的已注册 Agent（如 SCHEDULED 元数据 Agent）→ 409 不可运行。
AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "SUPPLIER_360_AGENT": ("supplier_360",),
    "SUPPLIER_RISK_AGENT": ("supplier_risk",),
    "GRAPH_REASONING_AGENT": ("graph_traverse",),
}


class AgentRuntimeService:
    """轻量 Agent 调度器：注册 → 工具路由 → 策略拦截 → 执行。"""

    def __init__(
        self,
        *,
        registry: AgentToolRegistry | None = None,
        agentService: AgentRegistryService | None = None,
    ) -> None:
        self._registry = registry or agent_tool_registry
        self._agents = agentService or AgentRegistryService()

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

        tool_names = AGENT_TOOLS.get(agent_code)
        # entity.status 是 String 列（存枚举 .value）；None 兜底给「无工具绑定」语义
        status_label = entity.status or "no_tool_binding"
        if entity.status != AgentStatus.ACTIVE.value or not tool_names:
            raise ConflictError(
                MSG_AGENT_NOT_RUNNABLE.format(code=agent_code, status=status_label)
            )

        tool = self._resolveTool(tool_names[0])
        self._enforcePolicies(entity, tool.data_object)

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

    def _enforcePolicies(self, entity: AgentDefinition, data_object: str) -> None:
        """deny-by-default：对 tool.data_object 需有 READ / MASKED_READ 策略。

        FORBIDDEN / FORBIDDEN_WRITE / 无策略 → PermissionDeniedError(403)。
        MASKED_READ 视为可访问（敏感字段脱敏留待后续 Phase）。
        """
        policy = next(
            (p for p in entity.policies if p.data_object == data_object), None
        )
        if policy is None or policy.permission not in (
            AgentPermission.READ.value,
            AgentPermission.MASKED_READ.value,
        ):
            raise PermissionDeniedError(
                MSG_AGENT_RUN_DENIED.format(
                    code=entity.agent_code, object=data_object
                )
            )
