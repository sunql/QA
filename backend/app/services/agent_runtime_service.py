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

import asyncio
import logging
from dataclasses import dataclass, field
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

        tool = await self._resolveTool(session, tool_name)
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

    async def _resolveTool(self, session: AsyncSession, name: str) -> AgentTool:
        """按名解析 AgentTool；cache miss 时 reload_one（DB-backed registry）。

        tool 不存在或被禁用 → ConflictError(409 tool_unbound_or_disabled)。
        """
        tool = self._registry.get(name)
        if tool is None:
            await self._registry.reload_one(session, name)
            tool = self._registry.get(name)
        if tool is None:
            raise ConflictError(
                MSG_AGENT_NOT_RUNNABLE.format(
                    code=name, status="tool_unbound_or_disabled"
                )
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


# =============================================================================
# L4 Agent Loop — run_agent_loop
# =============================================================================


@dataclass(frozen=True)
class AgentLoopResult:
    """L4 Agent Loop 返回值。"""

    final_sql: str | None
    answer_text: str | None
    iterations_used: int
    tool_calls_made: list[str]
    total_cost_usd: float
    terminated_reason: str  # "answered" | "max_iterations" | "cost_cap" | "error"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_llm_message(msg) -> "LlmMessage":
    """把 langchain BaseMessage 转为项目的 LlmMessage。"""
    from app.infrastructure.llm.base_client import LlmMessage

    if hasattr(msg, "type"):
        if msg.type == "human":
            return LlmMessage(role="user", content=msg.content or "")
        if msg.type == "ai":
            return LlmMessage(role="assistant", content=msg.content or "")
        if msg.type == "tool":
            return LlmMessage(role="tool", content=msg.content or "")
    # fallback
    return LlmMessage(role="user", content=str(msg))


def _estimate_cost(usage: dict | None) -> float:
    """根据 token 用量估算 cost（USD）。

    使用 openai gpt-4o-mini 参考价：$0.15/1M input, $0.6/1M output。
    """
    if not usage:
        return 0.0
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    return (prompt * 0.15 / 1_000_000) + (completion * 0.6 / 1_000_000)


def _extract_final_sql(messages: list) -> str | None:
    """从对话历史中提取 final_sql。

    约定：若 LLM 在最终 answer 中包含 ``final_sql`...` `` 格式，
    则从中取出 SQL 字符串。
    """
    import re

    for msg in reversed(messages):
        content = getattr(msg, "content", None) or ""
        # 匹配 ```final_sql ... ``` 或 ::final_sql:: ... ::
        match = re.search(
            r"(?:```final_sql|::final_sql::)\s*([\s\S]+?)(?:```|::)", content
        )
        if match:
            return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# AgentRuntimeService — run_agent_loop
# ---------------------------------------------------------------------------

QueryExecutor = object  # minimal type hint; the mock in tests has execute_read_only


async def run_agent_loop(
    self,
    *,
    session: AsyncSession,
    user_id: int,
    question: str,
    llm_client,
    executor,
    ontology,
    max_iterations: int = 5,
    cost_budget_usd: float = 0.5,
) -> AgentLoopResult:
    """LLM 驱动的 agent loop，自主探索 NL2SQL 答案。

    循环：
    1. agent_node: 调 LLM 决策（complete_with_tools）
    2. 若有 tool_calls → dispatch 每个 tool_call
    3. 终止条件：max_iterations / cost_cap / 无 tool_calls

    依赖注入（便于测试 mock）：
    - llm_client.complete_with_tools(messages, tools, tool_choice) -> LlmResponseWithTools
    - executor.execute_read_only(sql) -> list[dict]  （仅 execute_sql 工具路径用到）
    - ontology.listTables / describeTable / sampleRows / listJoins
    """
    from app.infrastructure.llm.base_client import LlmMessage
    from app.services.agent_tools_nl2sql import TOOL_SCHEMAS, dispatch_tool_call
    from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

    messages: list = [HumanMessage(content=question)]
    iterations = 0
    terminated_reason = "error"
    final_sql: str | None = None
    answer_text: str | None = None
    tool_calls_made: list[str] = []
    total_cost = 0.0

    while iterations < max_iterations:
        iterations += 1

        # Step 1: 调 LLM
        llm_messages = [_to_llm_message(m) for m in messages]
        response = await llm_client.complete_with_tools(
            messages=llm_messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )
        total_cost += _estimate_cost(response.usage)

        if total_cost > cost_budget_usd:
            terminated_reason = "cost_cap"
            break

        # Append AI message
        ai_msg = AIMessage(
            content=response.content or "",
            tool_calls=[
                {"id": tc.id, "name": tc.name, "args": tc.args}
                for tc in response.tool_calls
            ],
        )
        messages.append(ai_msg)

        # Step 2: 如果没有 tool_call，结束
        if not response.tool_calls:
            terminated_reason = "answered"
            answer_text = response.content
            final_sql = _extract_final_sql(messages)
            break

        # Step 3: 执行工具
        for tc in response.tool_calls:
            tool_calls_made.append(tc.name)

            # 把 ToolCall 适配为 dispatch_tool_call 需要的结构
            from app.infrastructure.llm.base_client import ToolCall as Tc

            tc_adapter = Tc(id=tc.id, name=tc.name, args=tc.args)

            if tc.name == "execute_sql":
                # execute_sql 经 executor 以支持 mock
                try:
                    sql = tc.args.get("sql", "")
                    rows = await executor.execute_read_only(sql)
                    import json

                    content = json.dumps(
                        {"rows": rows, "row_count": len(rows)},
                        ensure_ascii=False,
                        default=str,
                    )
                except Exception as exc:
                    import json

                    content = json.dumps(
                        {"error": type(exc).__name__, "detail": str(exc)},
                        ensure_ascii=False,
                    )
                messages.append(
                    ToolMessage(content=content, tool_call_id=tc.id, name=tc.name)
                )
            else:
                # 其余工具走 dispatch_tool_call（ontology-backed）
                result = await dispatch_tool_call(
                    tc_adapter,
                    session=session,
                )
                messages.append(
                    ToolMessage(
                        content=result.content,
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                )
    else:
        # while 完成但未 break → 超 max_iterations
        terminated_reason = "max_iterations"

    return AgentLoopResult(
        final_sql=final_sql,
        answer_text=answer_text,
        iterations_used=iterations,
        tool_calls_made=tool_calls_made,
        total_cost_usd=round(total_cost, 6),
        terminated_reason=terminated_reason,
    )


# monkey-patch onto AgentRuntimeService (keeps original class untouched)
AgentRuntimeService.run_agent_loop = run_agent_loop
