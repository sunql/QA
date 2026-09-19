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
import json
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
from app.infrastructure.business_db_pool import _assert_read_only
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

# L4 agent loop 系统提示：引导 LLM 区分"需要查数据的探索性问句"和"无需查数据的解释性
# 问句"。前者走 tool calling，后者直接文字作答（避免无限循环调工具）。
# 历史 bug：缺系统提示 → LLM 永远调用 tool_calls 直到 max_iterations → answer_text=None
# → chat_service 降级到 L2 fallback，L4 路径看似"被降级"实则 LLM 从未进入"answered"分支。
_L4_SYSTEM_PROMPT = """你是 NL2SQL 数据分析助手。会话可调用以下工具查询数据库：
- list_tables / describe_table / sample_rows / list_joins：探索 schema（**仅在确实需要时**）
- execute_sql：执行只读 SELECT 查询

行为规则：
1. **判断问题类型**：
   - 需要查数据库才能回答（聚合、明细、筛选、对比、为什么某个数据是这样）→ 调用工具
   - 概念性问题（什么是 / 解释 / 如何理解领域术语 / 如何使用系统）→ 直接文字作答，无需调工具
   - 含糊不清 → 先回答"我理解你要问的是 X"，再决定是否需要查数据
2. **迭代预算**（硬约束）：
   - 探索阶段（list_tables / list_joins / describe_table / sample_rows）**最多 1 轮**且并发执行；同轮内可同时发出多个工具调用
   - 拿到 schema 立即写 execute_sql，**不要反复 describe 同一表**
   - 多步骤问题（topN + 子分析 + 聚合）**必须用一次 execute_sql + CTE/子查询合并**，不要拆成多次单 SQL 依次查询
3. **回答语言**：与用户问题一致（默认中文）
4. **结束**：拿到足够信息后必须直接文字总结（**不要再发任何 tool_call**），纯文本响应即终止
5. **放弃条件**：若 1 轮探索 + 1 次 execute_sql 仍未拿到数据，请基于已有信息给出推断 + 明确说明数据缺口，**立即文字总结**，不要再调工具

历史反例：曾因 LLM 反复 describe_table 而耗尽 max_iterations，请严守上述预算。"""


def _build_system_message(prompt: str):
    """构造 system message，兼容 langchain 对象与 dict shim（dev venv 缺 langchain 时）。"""
    try:
        from langchain_core.messages import SystemMessage

        return SystemMessage(content=prompt)
    except ImportError:
        return {"role": "system", "content": prompt, "type": "system"}


def _to_llm_message(msg) -> "LlmMessage":
    """把 langchain BaseMessage 或 dev shim dict 转为项目的 LlmMessage。"""
    from app.infrastructure.llm.base_client import LlmMessage

    # 兼容 langchain 对象与 dev shim dict
    msg_type = getattr(msg, "type", None)
    msg_content = getattr(msg, "content", None)
    if msg_type is None and isinstance(msg, dict):
        msg_type = msg.get("type")
        msg_content = msg.get("content")

    if msg_type == "human":
        return LlmMessage(role="user", content=msg_content or "")
    if msg_type == "ai":
        # AI 消息可能带 tool_calls（langchain AIMessage.tool_calls / additional_kwargs）
        ai_tool_calls = (
            getattr(msg, "tool_calls", None)
            or (msg.get("tool_calls") if isinstance(msg, dict) else None)
        )
        ai_tool_calls_tup = (
            tuple(ai_tool_calls) if ai_tool_calls else None
        )
        return LlmMessage(
            role="assistant",
            content=msg_content or "",
            tool_calls=ai_tool_calls_tup,
        )
    if msg_type == "tool":
        # ToolMessage 必须保留 tool_call_id（OpenAI tool API 强约束）；
        # 历史 bug：未传导致 deepseek/openai 返回 400 'missing field tool_call_id'
        tool_call_id = (
            getattr(msg, "tool_call_id", None)
            or (msg.get("tool_call_id") if isinstance(msg, dict) else None)
        )
        tool_name = (
            getattr(msg, "name", None)
            or (msg.get("name") if isinstance(msg, dict) else None)
        )
        return LlmMessage(
            role="tool",
            content=msg_content or "",
            tool_call_id=tool_call_id,
            name=tool_name,
        )
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
        # 兼容 langchain AIMessage / ToolMessage 与 dev shim dict
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        content = content or ""
        # 匹配 ```final_sql ... ``` 或 ::final_sql:: ... ::
        match = re.search(
            r"(?:```final_sql|::final_sql::)\s*([\s\S]+?)(?:```|::)", content
        )
        if match:
            return match.group(1).strip()
    return None


def _extract_last_ai_content(messages: list) -> str | None:
    """从对话历史倒序寻找最后一条非空 AI 消息 content。

    用于 max_iterations 兜底：即便 LLM 最后一次响应触发了 tool_calls（被视为 continue），
    也可能伴随自然语言说明（如"基于以上结果..."），作为最终 answer_text 兜底。
    """
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", None)
        if msg_type is None and isinstance(msg, dict):
            msg_type = msg.get("type")
        if msg_type != "ai":
            continue
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content")
        if content and content.strip():
            return content
    return None


# ---------------------------------------------------------------------------
# AgentLoop step result (immutable dataclass per iteration)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AgentLoopStepResult:
    """单次 LLM 调用的结果。"""

    ai_message: Any  # AIMessage | None
    tool_calls_to_run: list[Any]  # list[ToolCall]
    answer_text: str | None
    final_sql: str | None
    cost_incurred: float
    should_stop: bool
    stop_reason: str  # "answered" | "cost_cap" | "continue"


# ---------------------------------------------------------------------------
# AgentRuntimeService — run_agent_loop
# ---------------------------------------------------------------------------

QueryExecutor = object  # minimal type hint; the mock in tests has execute_read_only


async def _runAgentLoopIteration(
    *,
    messages: list,
    llm_client,
    tool_schemas: list[dict],
    cost_budget_usd: float,
    cumulative_cost: float,
) -> _AgentLoopStepResult:
    """单次 LLM 决策迭代：调用 complete_with_tools，检查 cost cap。"""
    llm_messages = [_to_llm_message(m) for m in messages]
    response = await llm_client.complete_with_tools(
        messages=llm_messages,
        tools=tool_schemas,
        tool_choice="auto",
    )
    cost_incurred = _estimate_cost(response.usage)
    new_total = cumulative_cost + cost_incurred

    if new_total > cost_budget_usd:
        return _AgentLoopStepResult(
            ai_message=None,
            tool_calls_to_run=[],
            answer_text=None,
            final_sql=None,
            cost_incurred=cost_incurred,
            should_stop=True,
            stop_reason="cost_cap",
        )

    ai_message = _buildAiMessage(response)

    if not response.tool_calls:
        return _AgentLoopStepResult(
            ai_message=ai_message,
            tool_calls_to_run=[],
            answer_text=response.content,
            final_sql=None,
            cost_incurred=cost_incurred,
            should_stop=True,
            stop_reason="answered",
        )

    return _AgentLoopStepResult(
        ai_message=ai_message,
        tool_calls_to_run=response.tool_calls,
        answer_text=None,
        final_sql=None,
        cost_incurred=cost_incurred,
        should_stop=False,
        stop_reason="continue",
    )


def _buildAiMessage(response) -> Any:
    """把 LlmResponseWithTools 转 AIMessage（langchain）。dev venv 缺包时降级为 dict。

    tool_calls 序列化为 OpenAI 兼容格式（type=function + function.arguments 是 JSON 字符串），
    否则深求/openai 反序列化报错：messages[i]: unknown variant `tool_call`, expected `function`。
    AIMessage 用 langchain 的 tool_calls 结构（id/name/args），但其实只走 dict 兜底路径
    （langchain 只是类型注解占位，最终 _to_llm_message 序列化时按 OpenAI 格式）。
    """
    # OpenAI 兼容 tool_calls：{id, type:'function', function:{name, arguments(JSON 字符串)}}
    openai_tool_calls = [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": json.dumps(tc.args, ensure_ascii=False),
            },
        }
        for tc in response.tool_calls
    ]
    try:
        from langchain_core.messages import AIMessage
        return AIMessage(
            content=response.content or "",
            tool_calls=[
                {"id": tc.id, "name": tc.name, "args": tc.args}
                for tc in response.tool_calls
            ],
        )
    except ImportError:
        return {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": openai_tool_calls,
            "type": "ai",
        }


async def _dispatchSingleTool(
    *,
    tc,
    session: AsyncSession,
    executor,
) -> Any:  # ToolMessage
    """处理单个 tool_call → ToolMessage。"""
    from app.infrastructure.llm.base_client import ToolCall as Tc
    import json

    tc_adapter = Tc(id=tc.id, name=tc.name, args=tc.args)

    if tc.name == "execute_sql":
        try:
            sql = tc.args.get("sql", "")
            _assert_read_only(sql)
            rows = await executor.execute_read_only(sql)
            content = json.dumps(
                {"rows": rows, "row_count": len(rows)},
                ensure_ascii=False,
                default=str,
            )
        except Exception as exc:
            content = json.dumps(
                {"error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=False,
            )
    else:
        from app.services.agent_tools_nl2sql import dispatch_tool_call

        result = await dispatch_tool_call(tc_adapter, session=session)
        content = result.content

    try:
        from langchain_core.messages import ToolMessage
        return ToolMessage(content=content, tool_call_id=tc.id, name=tc.name)
    except ImportError:
        return {
            "role": "tool",
            "content": content,
            "tool_call_id": tc.id,
            "name": tc.name,
            "type": "tool",
        }


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
    """LLM 驱动的 agent loop（纯 Python async while 实现）。

    Architecture 偏差说明：本实现用纯 Python async while loop，未采用 LangGraph StateGraph，
    因为所有 handler 都是 async、StateGraph node 包装复杂且测试 mock 困难。
    AgentState TypedDict 保留为后续 LangGraph 升级占位（见 agent_state.py）。

    终止条件：answered / max_iterations / cost_cap / error
    """
    from app.services.agent_state import AgentState  # noqa: F401 — future LangGraph upgrade
    from app.services.agent_tools_nl2sql import TOOL_SCHEMAS

    _logLoopStart(user_id, question)

    state = _initLoopState(question)
    messages = state["messages"]

    while state["iterations"] < max_iterations:
        await _runOneStep(state, llm_client, TOOL_SCHEMAS, cost_budget_usd)

        if state["terminated_reason"] in ("answered", "cost_cap"):
            break

        await _executePendingToolCalls(
            state=state,
            session=session,
            executor=executor,
        )

    # max_iterations 兜底：若耗尽迭代但 LLM 在最后一轮仍有自然语言输出（即便伴生 tool_calls），
    # 用该内容作为 answer_text，避免 chat_service 因 answer_text is None 降级到 L2。
    # 历史 bug：5 次迭代都继续调工具 → answer_text=None → 路由 L2 看似生效实则 L4 失败。
    if (
        state["terminated_reason"] == "max_iterations"
        and state["answer_text"] is None
    ):
        fallback = _extract_last_ai_content(state["messages"])
        if fallback:
            state["answer_text"] = fallback

    _logLoopEnd(
        user_id=user_id,
        terminated_reason=state["terminated_reason"],
        iterations=state["iterations"],
        total_cost=state["total_cost"],
    )

    return AgentLoopResult(
        final_sql=state["final_sql"],
        answer_text=state["answer_text"],
        iterations_used=state["iterations"],
        tool_calls_made=state["tool_calls_made"],
        total_cost_usd=round(state["total_cost"], 6),
        terminated_reason=state["terminated_reason"],
    )


def _logLoopStart(user_id: int, question: str) -> None:
    """Audit log: loop start."""
    logger.info(
        "agent_loop.start user_id=%s question_len=%d",
        user_id,
        len(question),
    )


def _logLoopEnd(*, user_id: int, terminated_reason: str, iterations: int, total_cost: float) -> None:
    """Audit log: loop end."""
    logger.info(
        "agent_loop.end user_id=%s terminated=%s iterations=%d cost=%.6f",
        user_id,
        terminated_reason,
        iterations,
        total_cost,
    )


def _initLoopState(question: str) -> dict:
    """初始化 loop 状态。"""
    try:
        from langchain_core.messages import HumanMessage
        messages = [
            _build_system_message(_L4_SYSTEM_PROMPT),
            HumanMessage(content=question),
        ]
    except ImportError:
        # dev/test venv 可能缺 langchain_core；用 dict shim
        messages = [
            _build_system_message(_L4_SYSTEM_PROMPT),
            {"role": "user", "content": question, "type": "human"},
        ]

    return {
        "messages": messages,
        "iterations": 0,
        "total_cost": 0.0,
        "tool_calls_made": [],
        "terminated_reason": "max_iterations",
        "final_sql": None,
        "answer_text": None,
    }


async def _runOneStep(
    state: dict,
    llm_client,
    tool_schemas: list[dict],
    cost_budget_usd: float,
) -> None:
    """执行一次 LLM step；in-place 更新 state。"""
    step = await _runAgentLoopIteration(
        messages=state["messages"],
        llm_client=llm_client,
        tool_schemas=tool_schemas,
        cost_budget_usd=cost_budget_usd,
        cumulative_cost=state["total_cost"],
    )
    state["iterations"] += 1
    state["total_cost"] += step.cost_incurred

    if step.should_stop:
        state["terminated_reason"] = step.stop_reason
        if step.stop_reason == "answered":
            state["messages"].append(step.ai_message)
            state["answer_text"] = step.answer_text
            state["final_sql"] = _extract_final_sql(state["messages"])
        return

    state["messages"].append(step.ai_message)
    state["pending_tool_calls"] = step.tool_calls_to_run


async def _executePendingToolCalls(*, state: dict, session, executor) -> None:
    """执行 pending tool_calls；in-place 更新 state.tool_calls_made / messages。"""
    for tc in state.pop("pending_tool_calls", []):
        state["tool_calls_made"].append(tc.name)
        tm = await _dispatchSingleTool(tc=tc, session=session, executor=executor)
        state["messages"].append(tm)


# monkey-patch onto AgentRuntimeService (keeps original class untouched)
AgentRuntimeService.run_agent_loop = run_agent_loop
