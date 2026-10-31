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

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
from app.security.prompt_sanitizer import (
    UntrustedLabel,
    renderSecurityPreamble,
    wrapUntrusted,
    wrapUntrustedStructured,
)
from app.services.agent_binding_cache import agent_binding_cache
from app.services.agent_registry_service import AgentRegistryService
from app.services.agent_tool_config_registry import (
    AgentToolConfigRegistry,
    agent_tool_config_registry,
)
from app.services.agent_tools import (
    AgentTool,
    AgentToolContext,
    AgentToolRegistry,
)
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
        llm_config: Any | None = None,
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

        ctx = AgentToolContext(
            llm_factory=llm_factory, llm_config=llm_config, actor=actor
        )
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
            executed_at=datetime.now(UTC),
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

    def enforcePolicies(self, entity: AgentDefinition, tool: AgentTool) -> None:
        """Public delegate for separate runtimes; policy semantics remain here."""
        self._enforcePolicies(entity, tool)

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
    # 各轮 complete_with_tools 的用量累加值。调用方据此落 session_token_usage
    # 台账（核心约束 #3）——此前 Result 只有一个 USD 数字，token 数无从承载，
    # L4 的消耗因此整条游离在台账之外。
    prompt_tokens: int
    completion_tokens: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# L4 agent loop 系统提示：引导 LLM 区分"需要查数据的探索性问句"和"无需查数据的解释性
# 问句"。前者走 tool calling，后者直接文字作答（避免无限循环调工具）。
# 历史 bug：缺系统提示 → LLM 永远调用 tool_calls 直到 max_iterations → answer_text=None
# → chat_service 降级到 L2 fallback，L4 路径看似"被降级"实则 LLM 从未进入"answered"分支。
_L4_SYSTEM_PROMPT = (
    renderSecurityPreamble()
    + "\n\n"
    + """你是 NL2SQL 数据分析助手。会话可调用以下工具查询数据库：
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
3. **工具结果阅读**：
   - describe_table 的 columns 是数组行，字段顺序见返回的 column_format；list_joins 只给涉及的表（tables 参数过滤）
   - execute_sql 的 rows 与 columns 按位置对应；若带 truncated=true，仅前 100 行可见——**不要用部分行推总数**，需要的聚合/计数请写进 SQL
4. **回答语言**：与用户问题一致（默认中文）
5. **结束**：拿到足够信息后必须直接文字总结（**不要再发任何 tool_call**），纯文本响应即终止
6. **放弃条件**：若 1 轮探索 + 1 次 execute_sql 仍未拿到数据，请基于已有信息给出推断 + 明确说明数据缺口，**立即文字总结**，不要再调工具

历史反例：曾因 LLM 反复 describe_table 而耗尽 max_iterations，请严守上述预算。"""
)


def _build_system_message(prompt: str):
    """构造 system message，兼容 langchain 对象与 dict shim（dev venv 缺 langchain 时）。"""
    try:
        from langchain_core.messages import SystemMessage

        return SystemMessage(content=prompt)
    except ImportError:
        return {"role": "system", "content": prompt, "type": "system"}


def _to_llm_message(msg) -> LlmMessage:
    """把 langchain BaseMessage 或 dev shim dict 转为项目的 LlmMessage。"""
    from app.infrastructure.llm.base_client import LlmMessage

    # 兼容 langchain 对象与 dev shim dict
    msg_type = getattr(msg, "type", None)
    msg_content = getattr(msg, "content", None)
    if msg_type is None and isinstance(msg, dict):
        msg_type = msg.get("type")
        msg_content = msg.get("content")

    if msg_type == "system":
        # 缺此分支时 system 会掉进 fallback 的 str(msg)，以 user 角色 + repr 形态
        # 发给 provider（指令层级错误 + 每轮多付一份 repr 噪声）
        return LlmMessage(role="system", content=msg_content or "")
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


def _usage_tokens(usage: dict | None) -> tuple[int, int]:
    """从 ``LlmResponseWithTools.usage`` 取 ``(promptTokens, completionTokens)``。

    缺失 / 非数值一律回 0（provider 未返回 usage 时不能凭空编数）。
    """
    if not usage:
        return 0, 0
    return (
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
    )


def _cost_for_usage(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cost_per_1k_input: float,
    cost_per_1k_output: float,
) -> Decimal:
    """按 model config 单价算 USD 成本（与 chat_service._costFor 同公式同口径）。

    单价由调用方从**真实 model config** 传入——agent loop 内部无从得知用的是哪个
    模型，此前硬编码 gpt-4o-mini 参考价的写法会让监控埋点上的 L4 成本与
    llm_config 里的单价脱钩（换模型后数字系统性失真，H2/H9 同类问题）。
    """
    return (
        Decimal(prompt_tokens) * Decimal(str(cost_per_1k_input))
        + Decimal(completion_tokens) * Decimal(str(cost_per_1k_output))
    ) / Decimal(1000)


def _lastExecutedSql(state: dict) -> str | None:
    """兜底 final_sql：取 loop 中最后一条**成功执行**的 execute_sql。

    L4 的系统提示词不要求模型输出围栏，实操上模型也几乎不会主动输出
    ``final_sql`` ⇒ 只靠 `_extract_final_sql` 时前端永远拿不到 SQL。
    这里以「真正跑通过的那条」为准；执行失败的 SQL 不记录（见
    `_dispatchSingleTool` 只在 execute_read_only 返回后追加）。
    """
    executed = state.get("executed_sqls") or []
    return executed[-1] if executed else None


def _extract_final_sql(messages: list) -> str | None:
    """从对话历史中提取 final_sql。

    约定：若 LLM 在最终 answer 中包含 ``final_sql`...` `` 格式，
    则从中取出 SQL 字符串。未命中时由 ``_lastExecutedSql`` 兜底。
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
    # 本轮用量：即便 cost 撞上限（cost_cap 提前 return）也必须带出来——
    # 那一轮的钱已经花了，会计上不能因为「本轮没产出答案」就把它抹掉。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # provider 报告的 prompt cache 命中 token；None = 字段缺失/不支持（≠ 0）
    cached_tokens: int | None = None


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
    cost_per_1k_input: float,
    cost_per_1k_output: float,
    run_id: str | None = None,
    session_id: str | None = None,
    iteration: int = 0,
) -> _AgentLoopStepResult:
    """单次 LLM 决策迭代：调用 complete_with_tools，检查 cost cap。"""
    llm_messages = [_to_llm_message(m) for m in messages]
    response = await llm_client.complete_with_tools(
        messages=llm_messages,
        tools=tool_schemas,
        tool_choice="auto",
    )
    prompt_tokens, completion_tokens = _usage_tokens(response.usage)
    cached_tokens = getattr(response, "cachedTokens", None)
    _logIterationTokenProfile(
        run_id=run_id,
        session_id=session_id,
        iteration=iteration,
        response=response,
        messages=llm_messages,
        tool_schemas=tool_schemas,
    )
    cost_incurred = float(
        _cost_for_usage(
            prompt_tokens,
            completion_tokens,
            cost_per_1k_input=cost_per_1k_input,
            cost_per_1k_output=cost_per_1k_output,
        )
    )
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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        )

    return _AgentLoopStepResult(
        ai_message=ai_message,
        tool_calls_to_run=response.tool_calls,
        answer_text=None,
        final_sql=None,
        cost_incurred=cost_incurred,
        should_stop=False,
        stop_reason="continue",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
    )


def _logToolResultProfile(
    *,
    run_id: str | None,
    session_id: str | None,
    iteration: int,
    tool_call_id,
    tool_name: str,
    raw_content: str,
    sent_content: str,
    row_count: int | None,
) -> None:
    """工具结果 raw vs sent 探针（转义放大 + 行数）。同迭代探针：失败只留 warning。"""
    from app.services import l4_token_profile

    if not logger.isEnabledFor(logging.INFO):
        return
    try:
        l4_token_profile.logToolResultProfile(
            l4_token_profile.buildToolResultProfile(
                runId=run_id,
                sessionId=session_id,
                iteration=iteration,
                toolCallId=str(tool_call_id) if tool_call_id is not None else None,
                toolName=tool_name,
                rawContent=raw_content,
                sentContent=sent_content,
                rowCount=row_count,
            )
        )
    except Exception:
        logger.warning(
            "agent_loop.token_profile_failed (tool_result tool=%s)", tool_name, exc_info=True
        )


def _logIterationTokenProfile(
    *,
    run_id: str | None,
    session_id: str | None,
    iteration: int,
    response,
    messages: list,
    tool_schemas: list[dict],
) -> None:
    """逐轮 token 构成探针（纯观测）。度量绝不允许打断循环：任何异常只留 warning。"""
    from app.services import l4_token_profile

    if not logger.isEnabledFor(logging.INFO):
        return
    try:
        profile = l4_token_profile.buildIterationProfile(
            runId=run_id,
            sessionId=session_id,
            iteration=iteration,
            modelName=getattr(response, "model", "") or "",
            messages=messages,
            toolSchemas=tool_schemas,
            usage=response.usage,
            cachedTokens=getattr(response, "cachedTokens", None),
        )
        l4_token_profile.logIterationProfile(profile)
    except Exception:
        logger.warning(
            "agent_loop.token_profile_failed run_id=%s iteration=%d",
            run_id,
            iteration,
            exc_info=True,
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
    run_id: str | None = None,
    session_id: str | None = None,
    iteration: int = 0,
    executed_sqls: list[str] | None = None,
) -> Any:  # ToolMessage
    """处理单个 tool_call → ToolMessage。

    ``executed_sqls``：成功执行的 SQL 累积列表（调用方持有），
    供 loop 结束时兜底回填 final_sql；失败的 SQL 不进入。
    """
    import json

    from app.infrastructure.llm.base_client import ToolCall as Tc

    tc_adapter = Tc(id=tc.id, name=tc.name, args=tc.args)

    rowCount: int | None = None
    if tc.name == "execute_sql":
        try:
            sql = tc.args.get("sql", "")
            _assert_read_only(sql)
            rows = await executor.execute_read_only(sql)
            rowCount = len(rows)
            if executed_sqls is not None:
                executed_sqls.append(sql.strip())
            # 列式 + 行数上限：执行器取全量，只截喂给 LLM 的序列化（rowCount 仍真值）
            from app.services.agent_tools_nl2sql import encodeExecuteSqlResult

            content = json.dumps(
                encodeExecuteSqlResult(rows),
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

    # 工具结果是 JSON 数据：只转 & < >（引号保留），避免 &quot; 放大 + 保持可读
    safeContent = wrapUntrustedStructured(UntrustedLabel.AGENT_TOOL_RESULT, content)
    _logToolResultProfile(
        run_id=run_id,
        session_id=session_id,
        iteration=iteration,
        tool_call_id=tc.id,
        tool_name=tc.name,
        raw_content=content,
        sent_content=safeContent,
        row_count=rowCount,
    )
    try:
        from langchain_core.messages import ToolMessage
        return ToolMessage(content=safeContent, tool_call_id=tc.id, name=tc.name)
    except ImportError:
        return {
            "role": "tool",
            "content": safeContent,
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
    # 2026-10-31：5 → 10。「为什么」类归因问题实测耗 5 轮在探表/试错 SQL 上
    # （list_tables + describe_table×3 + execute_sql×3），第 5 轮就到顶、从未进入
    # 写结论的轮次，用户只拿到 [L4:max_iterations] 前缀的半句话。成本仍由
    # cost_budget_usd 兜底（默认 0.5 USD），迭代上限只决定「有预算时能否收敛」。
    max_iterations: int = 10,
    cost_budget_usd: float = 0.5,
    cost_per_1k_input: float = 0.0,
    cost_per_1k_output: float = 0.0,
    sessionId: str | None = None,
) -> AgentLoopResult:
    """LLM 驱动的 agent loop（纯 Python async while 实现）。

    Architecture 偏差说明：本实现用纯 Python async while loop，未采用 LangGraph StateGraph，
    因为所有 handler 都是 async、StateGraph node 包装复杂且测试 mock 困难。
    AgentState TypedDict 保留为后续 LangGraph 升级占位（见 agent_state.py）。

    ``cost_per_1k_input`` / ``cost_per_1k_output``：来自**调用方解析出的 model config**，
    本函数无从得知实际用哪个模型，故单价必须由外部传入。二者为 0（默认）时
    ``total_cost_usd`` 恒为 0——调用方须显式传真实单价，否则 L4 成本会被静默低估。

    终止条件：answered / max_iterations / cost_cap / error
    """
    from app.services.agent_state import AgentState  # noqa: F401 — future LangGraph upgrade
    from app.services.agent_tools_nl2sql import TOOL_SCHEMAS

    _logLoopStart(user_id, question)

    state = _initLoopState(question)
    # 探针归因字段：runId 串起逐轮 profile / 工具结果行 / loop_end 汇总
    state["run_id"] = uuid.uuid4().hex[:12]
    state["session_id"] = sessionId
    state["cached_tokens"] = None
    messages = state["messages"]

    while state["iterations"] < max_iterations:
        try:
            await _runOneStep(
                state,
                llm_client,
                TOOL_SCHEMAS,
                cost_budget_usd,
                cost_per_1k_input=cost_per_1k_input,
                cost_per_1k_output=cost_per_1k_output,
            )

            if state["terminated_reason"] in ("answered", "cost_cap"):
                break

            await _executePendingToolCalls(
                state=state,
                session=session,
                executor=executor,
            )
        except Exception:
            # 终止原因 error：中途失败（限流 / 超时 / 工具派发异常）时，**前面若干轮
            # 的钱已经花了**。这里必须收敛成「带累计 token 的正常返回」，而不是让
            # 异常穿出去——调用方（chat_service._runL4AgentLoop）的 except 在
            # _recordUsage 之前 return，异常一穿出去累计值就随之丢失，L4 的这部分
            # 花费既不在台账、也不在监控埋点里（核心约束 #3）。
            # 行为不变：结果 terminated_reason="error" → answer_text 为 None →
            # _maybeRunL4AgentLoop 照旧降级 L2/L3。
            logger.warning(
                "agent_loop 迭代失败，按 error 终止（保留已消耗 token 以落台账）",
                exc_info=True,
            )
            state["terminated_reason"] = "error"
            break

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

    # final_sql 兜底：显式围栏优先（answered 分支已取），否则回显最后一条**成功执行**
    # 的 SQL。实际被消费的终止原因是 answered 与 max_iterations（error / cost_cap 在
    # chat_l4 侧会降级 L2/L3，final_sql 随之丢弃）；这里统一算，避免两条分支各写一遍。
    if not state["final_sql"]:
        state["final_sql"] = _lastExecutedSql(state)

    _logLoopEnd(
        user_id=user_id,
        terminated_reason=state["terminated_reason"],
        iterations=state["iterations"],
        total_cost=state["total_cost"],
    )

    from app.services import l4_token_profile

    if logger.isEnabledFor(logging.INFO):
        try:
            l4_token_profile.logLoopEndProfile(
                l4_token_profile.buildLoopEndProfile(
                    runId=state.get("run_id"),
                    sessionId=state.get("session_id"),
                    modelName=getattr(llm_client, "modelName", "") or "",
                    iterations=state["iterations"],
                    terminatedReason=state["terminated_reason"],
                    promptTokens=state["prompt_tokens"],
                    completionTokens=state["completion_tokens"],
                    cachedTokens=state.get("cached_tokens"),
                )
            )
        except Exception:
            logger.warning("agent_loop.token_profile_failed (loop_end)", exc_info=True)

    return AgentLoopResult(
        final_sql=state["final_sql"],
        answer_text=state["answer_text"],
        iterations_used=state["iterations"],
        tool_calls_made=state["tool_calls_made"],
        total_cost_usd=round(state["total_cost"], 6),
        terminated_reason=state["terminated_reason"],
        prompt_tokens=state["prompt_tokens"],
        completion_tokens=state["completion_tokens"],
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
    """初始化 loop 状态。L1 防御：question 用 <user_question> 包裹。"""
    safeQuestion = wrapUntrusted(UntrustedLabel.USER_QUESTION, question)
    try:
        from langchain_core.messages import HumanMessage
        messages = [
            _build_system_message(_L4_SYSTEM_PROMPT),
            HumanMessage(content=safeQuestion),
        ]
    except ImportError:
        # dev/test venv 可能缺 langchain_core；用 dict shim
        messages = [
            _build_system_message(_L4_SYSTEM_PROMPT),
            {"role": "user", "content": safeQuestion, "type": "human"},
        ]

    return {
        "messages": messages,
        "iterations": 0,
        "total_cost": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls_made": [],
        "executed_sqls": [],
        "terminated_reason": "max_iterations",
        "final_sql": None,
        "answer_text": None,
    }


async def _runOneStep(
    state: dict,
    llm_client,
    tool_schemas: list[dict],
    cost_budget_usd: float,
    *,
    cost_per_1k_input: float,
    cost_per_1k_output: float,
) -> None:
    """执行一次 LLM step；in-place 更新 state。"""
    step = await _runAgentLoopIteration(
        messages=state["messages"],
        llm_client=llm_client,
        tool_schemas=tool_schemas,
        cost_budget_usd=cost_budget_usd,
        cumulative_cost=state["total_cost"],
        cost_per_1k_input=cost_per_1k_input,
        cost_per_1k_output=cost_per_1k_output,
        run_id=state.get("run_id"),
        session_id=state.get("session_id"),
        iteration=state["iterations"] + 1,
    )
    state["iterations"] += 1
    state["total_cost"] += step.cost_incurred
    state["prompt_tokens"] += step.prompt_tokens
    state["completion_tokens"] += step.completion_tokens
    if step.cached_tokens is not None:
        state["cached_tokens"] = (state.get("cached_tokens") or 0) + step.cached_tokens

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
        tm = await _dispatchSingleTool(
            tc=tc,
            session=session,
            executor=executor,
            run_id=state.get("run_id"),
            session_id=state.get("session_id"),
            iteration=state["iterations"],
            executed_sqls=state["executed_sqls"],
        )
        state["messages"].append(tm)


# monkey-patch onto AgentRuntimeService (keeps original class untouched)
AgentRuntimeService.run_agent_loop = run_agent_loop
