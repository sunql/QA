"""L4 Agent Loop（ChatService 的 L4Mixin）。

探索性问句（为什么/怎么算/拆解/解释…）的兜底路由：命中时把问题交给 Agent 运行时
（AgentRuntimeService.run_agent_loop）走带工具调用的多轮循环，产出结构化答案；任何
失败（配置缺失 / 密文损坏 / agent loop 抛错 / 未开启）都返回 None 降级到 L2/L3 主链路。
含 L4 的 LLM 计量落账（核心约束 #3 —— 失败路径也是计量路径）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import IntentType
from app.domain.exceptions import ConfigError
from app.domain.schemas import ChatRequest, ChatResponse
from app.infrastructure.llm.base_client import BaseLlmClient
from app.services.agent_runtime_service import AgentLoopResult
from app.services.chat_constants import (
    ROUTING_LAYER_L4,
    USAGE_PURPOSE_L4_AGENT_LOOP,
)

logger = logging.getLogger(__name__)


class L4Mixin:
    """L4 Agent Loop（由 ChatService 组合）。"""

    # 探索性关键词：触发起 L4 Agent Loop（而非直接走 L2/L3 NL2SQL）
    _L4_EXPLORATORY_KEYWORDS = ("为什么", "怎么算", "拆解", "解释", "如何", "是什么",
                                  "为什么是", "为什么说", "如何计算", "如何分析")

    async def _handleNl2SqlAgent(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        *,
        user: CurrentUser | None = None,
    ) -> ChatResponse | None:
        """L4 入口：判断是否走 Agent Loop（兜底路由）。

        触发条件（同时满足）：
        1. ENABLE_L4_AGENT_LOOP='true'（system_config 表）
        2. 问题含探索性关键词（为什么/怎么算/拆解/解释/如何...）

        返回 ChatResponse = 命中 L4（LLM 已跑完）；返回 None = 降级到 L2/L3。

        L4 失败时同样返回 None，确保不阻断主链路（与 L1 KPI 同模式）。
        """
        if not await self._isL4AgentLoopEnabled(session):
            return None

        # 探索性关键词检测（简单 substring，不过度设计；详见 _L4_EXPLORATORY_KEYWORDS 定义处）
        if not any(kw in dto.question for kw in self._L4_EXPLORATORY_KEYWORDS):
            return None

        result = await self._runL4AgentLoop(session=session, dto=dto, user=user)
        if result is None or result.terminated_reason == "error" or result.answer_text is None:
            return None

        return await self._buildL4ChatResponse(session=session, dto=dto, result=result)

    async def _runL4AgentLoop(
        self,
        *,
        session: AsyncSession,
        dto: ChatRequest,
        user: CurrentUser | None,
    ) -> AgentLoopResult | None:
        """调 AgentRuntimeService.run_agent_loop；异常返回 None（降级）。"""
        # 先按 dto.modelId / router 解析 LLM 客户端（与 _buildPipelineContext 同模式）。
        # 历史 bug：传 None 给 createClient 走 OPENAI + env openaiApiKey 路径，本项目未配置
        # 该 env → 永远 None → agent loop `llm_client.complete_with_tools` 抛 AttributeError
        # → 全部降级 L2/L3。修复：始终 resolve 出 config 对象再交给工厂。
        resolved = await self._resolveChatLlmClient(session, dto)
        if resolved is None:
            logger.warning("L4 skipped: no usable LLM client (modelId=%s)", dto.modelId)
            return None
        llm_client, llm_config = resolved
        try:
            result = await self._agentRuntime.run_agent_loop(
                session=session,
                user_id=user.userId if user else 0,
                question=dto.question,
                llm_client=llm_client,
                executor=self._adapterProvider(dto.datasourceId, None),  # 懒加载 adapter
                ontology=self._ontology,
                # 单价的唯一来源是本次实际使用的 model config：agent loop 内部
                # 无从得知用了哪个模型（历史实现硬编码 gpt-4o-mini 参考价，与实际
                # config 单价脱钩，监控埋点上的 L4 成本随之失真）。
                cost_per_1k_input=float(llm_config.cost_per_1k_input),
                cost_per_1k_output=float(llm_config.cost_per_1k_output),
            )
        except Exception:
            # L4 异常不阻断：log warning + 降级 L2/L3（与 L1 同模式）
            logger.warning("L4 agent loop failed, falling back to L2/L3", exc_info=True)
            return None

        # 计量（核心约束 #3）：L4 是全项目最后一条「花钱不记账」的路径。此前
        # AgentLoopResult 只有一个 USD 数字、token 数无处承载 ⇒ 台账零记录，L4 的
        # 消耗不进会话成本报表，模型路由的预算降级判断也随之低估。
        # 记账不管调用方是否采纳本轮结果——tokens 已经花掉了，即便随后因
        # answer_text 为空而降级 L2，这笔账也必须留在台账上。
        if result.prompt_tokens or result.completion_tokens:
            await self._recordUsage(
                session,
                dto.sessionId,
                llm_config,
                result.prompt_tokens,
                result.completion_tokens,
                purpose=USAGE_PURPOSE_L4_AGENT_LOOP,
            )
        return result

    async def _resolveChatLlmClient(
        self,
        session: AsyncSession,
        dto: ChatRequest,
    ) -> tuple[BaseLlmClient, Any] | None:
        """解析「单次 LLM 调用」用的客户端 + 配置：dto.modelId 优先，否则 router 选。

        供**不带流水线上下文**的分支使用（L4 agent loop / 供应商风险点 / Agent 运行时）
        ——这些分支拿不到 ``_PipelineContext.selected``，又都需要「客户端」和「真实单价」
        两样东西，故一次解析一并返回。

        返回 ``(client, config)``：config 必须交回调用方，因为下游的计量（token 台账、
        监控埋点的 USD 成本）只能由它的 ``id`` / ``cost_per_1k_*`` 提供。

        返回 None = 无可用配置 → 调用方自行降级（L4 → L2/L3，供应商风险 → 模板文案）。
        自动路由同样收窄到可用池，避免降级分支选中无 key 的配置。
        """
        configs = await self._listModelConfigs(session)
        if dto.modelId is not None:
            selected = next((c for c in configs if c.id == dto.modelId), None)
            if selected is None or not selected.is_active:
                return None
        else:
            candidates = self._usableModelConfigs(configs)
            if not candidates:
                # LlmConfig 表为空（未 seed / 全停用）：自动路由的 selectModel([]) 会抛
                # NoAvailableModelError。本方法对三个调用方都是**可降级**契约，不能让
                # 「没有模型配置」把一条本来能友好作答的请求打成 400——返回 None 交调用方
                # 决定降级（L4 → L2/L3，供应商风险 → 模板文案）。
                return None
            ctx = await self._buildRoutingContext(session, dto.sessionId)
            selected = self._modelRouter.selectModel(candidates, dto.question, ctx)
        try:
            client = self._llmFactory(selected)
        except ConfigError as exc:
            # 本方法是**可降级**路径（契约：无可用配置 → 返回 None 交调用方降级），
            # 密文损坏不应把整轮请求打成 400。
            logger.warning(
                "可降级路径 skipped: 模型配置 id=%s 不可用（%s）",
                getattr(selected, "id", None), exc,
            )
            return None
        return client, selected

    async def _buildL4ChatResponse(
        self,
        *,
        session: AsyncSession,
        dto: ChatRequest,
        result: AgentLoopResult,
    ) -> ChatResponse:
        """把 AgentLoopResult wrap 成 ChatResponse + 持久化 + audit log。"""
        answer_text = f"[L4:{result.terminated_reason}] {result.answer_text}"
        response = ChatResponse(
            answer=answer_text,
            intent=IntentType.NEW_QUERY.value,
            sql=result.final_sql,
        )
        _t0 = time.monotonic()
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, response.answer, result.final_sql,
            routing_layer=ROUTING_LAYER_L4,
            latency_ms=int((time.monotonic() - _t0) * 1000),
            token_cost_usd=float(result.total_cost_usd),
        )
        logger.info(
            "L4 agent loop hit: iterations=%d cost=%.4f tool_calls=%s terminated=%s",
            result.iterations_used,
            result.total_cost_usd,
            result.tool_calls_made,
            result.terminated_reason,
        )
        return response

    async def _isL4AgentLoopEnabled(self, session: AsyncSession) -> bool:
        """读 system_config 表判断 L4 是否开启。

        查询 SELECT value FROM system_config WHERE key='ENABLE_L4_AGENT_LOOP'。
        表不存在 / 查不到 / 值为 'false' 时返回 False（安全默认值）。
        查询失败时也返回 False（不阻断主链路）。
        """
        try:
            query = text(
                "SELECT value FROM system_config WHERE key = 'ENABLE_L4_AGENT_LOOP'"
            )
            row = await session.execute(query)
            value = row.scalar_one_or_none()
            return value == "true"
        except Exception:
            logger.warning("Failed to read ENABLE_L4_AGENT_LOOP config", exc_info=True)
            return False
