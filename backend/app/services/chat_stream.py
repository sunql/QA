"""流式输出（ChatService 的 StreamMixin）。

一条消息的 SSE 流式编排：意图路由（meta）→ 闲聊/澄清/拦截卡片/查询/多步 各分支的
事件序列 → token×N → done，以及断连兜底 `persistInterruptedStream`（H4）。复用
非流式 handler 保证两条路径语义一致；领域异常收敛为结构化 error 事件（4-1）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import ChartType, IntentType
from app.domain.exceptions import (
    DomainError,
    LlmClientError,
    Nl2SqlError,
    ValidationError,
)
from app.domain.models import SessionQueryState
from app.domain.multi_step_plan import (
    GlobalFilters,
    MultiStepPlan,
    StepExecutionContext,
    StepResult,
)
from app.domain.query_plan import QueryPlan
from app.domain.schemas import AgentSuggestion, ChatRequest, SemanticState
from app.services import multi_step_persistence as persistence
from app.services.chat_constants import (
    ROUTING_LAYER_L2,
    USAGE_PURPOSE_ANSWER,
    USAGE_PURPOSE_NL2SQL,
)
from app.services.chart_thresholds import loadFullDataThreshold
from app.services.chat_helpers import (
    _MSG_STEP_AGGREGATION_SKIPPED,
    _clipText,
    _failedStepResult,
    _hasDataStepResult,
    _looks_like_compound_question,
    _PipelineContext,
    _step_result_to_read,
    attachStreamPersistState,
    streamPersistStateOf,
)
from app.services.evidence_record_service import (
    resetChatSessionId,
    resetChatUserId,
    setChatSessionId,
    setChatUserId,
)
from app.services.intent_service import IntentResult
from app.services.messages_zh import (
    MSG_INTERNAL_ERROR,
    MSG_STREAM_INTERRUPTED_EMPTY,
)
from app.services.multi_step_persist_hooks import _maxInputTokens, runStatusFor
from app.services.multi_step_retry import ERROR_KIND_PERMANENT
from app.services.nl2sql_semantic_guard import shareAmbiguityWarning

# 4-1（feat-token-cache）：_readFloatConfig 用于 LLM_CACHE_HIT_MULTIPLIER，
# 与 chat_service.processMessage 同口径——在 _streamQuery 入口一次性读一次，
# 整条流水线复用，避免每段 _costFor 调用都查 DB。
from app.services.nl2sql_service import _readFloatConfig
from app.services.stream_events import (
    EVENT_CHART,
    EVENT_CLASS_RECALL,
    EVENT_DATA_QUALITY,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_MULTI_STEP_PLAN,
    EVENT_PLAN,
    EVENT_SQL,
    EVENT_STEP_COMPRESSED,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
    ErrorType,
    StreamEvent,
)
from app.services.think_block import ThinkStreamFilter, applyThinkPolicy, isThinkHideEnabled
from app.services.visual_rationale import summaryTextOnlyRationale

logger = logging.getLogger(__name__)

# F7/IMP-6：概览回放的 DB 步状态 → 前端 `MultiStepStep.status` 终态映射。
# **只有**这两个会被回放成「已完成」：`prepareResume` 的前序步守卫只放行
# succeeded / compressed（multi_step_resume.py），故 `index < startIndex` 的步
# 必落其中之一。`failed` / `skipped` / `pending` / `running` 一律不回放。
_OVERVIEW_TERMINAL_STATUS: dict[str, str] = {
    "succeeded": "done",
    "compressed": "done",
}


def _overviewStepPayload(step, stepsByIdx: dict, startIndex: int) -> dict:
    """`multi_step_plan` 概览里单步的载荷。

    续跑时 `index < startIndex` 的步**不执行**（只在循环里 `continue`、不发任何事件），
    概览若一律下发 pending，这些**已成功**的步会在 UI 上从「已完成」退回「待执行」——
    「续跑省掉重跑」完全看不出来（DB 里它们确实是 succeeded）。故对它们回放**已持久化
    的终态**（唯一事实来源 = DB，不是前端记忆）。

    `>= startIndex` 的步（含汇总步）即将（重）跑，**不**带 status，由前端默认为
    pending —— 否则会把上一轮的失败当成本次终态显示。
    """
    payload: dict = {
        "stepIndex": step.index,
        "description": step.description,
        "subQuestion": step.sub_question,
        "aggregationOnly": step.aggregation_only,
    }
    if step.index < startIndex:
        persisted = stepsByIdx.get(step.index)
        replay = _OVERVIEW_TERMINAL_STATUS.get(getattr(persisted, "status", None))
        if replay is not None:
            payload["status"] = replay
    return payload


class StreamMixin:
    """流式输出（由 ChatService 组合）。"""

    async def processMessageStream(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        *,
        user: CurrentUser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式处理一条消息（入口包装：透传 chat session_id 到 evidence 记录）。

        v3.1 B2：流式链路内 execute_read_only 自动落 SQL_QUERY evidence，
        session_id 经 contextvar 透传（evidence_record_service）。生成器体内
        设置/复位：value 在查询调用栈里同步可见，断连/关闭时 finally 复位。
        R2：同时透传服务端 actor，chat 消息落库时打归属标（session_message.user_id）。
        """
        token = setChatSessionId(dto.sessionId)
        userToken = setChatUserId(user.userId if user is not None else None)
        try:
            async for event in self._processMessageStreamInner(dto, session, user=user):
                yield event
        finally:
            resetChatUserId(userToken)
            resetChatSessionId(token)

    async def _processMessageStreamInner(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        *,
        user: CurrentUser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式处理一条消息。

        meta 事件最先产出（告知意图）；有上一轮状态时重新分类并二次产出 meta。
        chitchat 直接产出一条问候；DEFINE/MAP/METRIC 走 _streamDomainCommand（零 LLM）；
        CLARIFY 走 _streamClarify；拦截类意图（360°/风险/图推理/Agent 运行）走
        _streamInterceptCard（#207 审查 HIGH 修复：此前流式路径从不路由这些意图，
        默认 streaming UI 下卡片从未渲染）；其余走 _streamQuery。
        查询流水线中的领域异常转为 error 事件（4-1 携带 errorType），保证 SSE
        始终以结构化事件结束。4-4：闲聊/出错轮也持久化消息，历史链不断。

        user 可选（#207 安全修复）：API 层透传真实调用方，Agent 运行用它作 actor。

        H4：函数入口即武装断连兜底（`StreamPersistState.pending = True`）。客户端在
        任何一次 yield 之后断连都可能已经看到内容，而断连时生成器停在 `yield` 上时
        `finally` 不会触发 ⇒ 只有响应收尾的 background 钩子能补写（见
        `persistInterruptedStream`）。任何一次成功落库都会自动解除（不会重复写）。
        """
        persistState = attachStreamPersistState(session)
        persistState.pending = True
        persistState.startedAt = time.monotonic()
        # Phase 6.5：supplier name → code 预解析（同 processMessage；流式入口覆盖）
        try:
            dto = await self._prepareSupplierQuestion(session, dto)
        except ValidationError as exc:
            # 流式惯例（与下方 DomainError 分支同型）：结构化 error 事件
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, exc.message, None
            )
            yield StreamEvent(
                EVENT_ERROR,
                {
                    "error": exc.message,
                    "errorType": ErrorType.DOMAIN.value,
                    "detail": exc.detail,
                },
            )
            return
        except Exception as exc:
            # 非预期失败（DB 连接中断等）：同样以结构化 error 事件结束 SSE，
            # 避免连接被强行中断（与 processMessageStream 文档保证一致）。
            logger.exception("supplier name 预解析失败: %s", exc)
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, MSG_INTERNAL_ERROR, None
            )
            yield StreamEvent(
                EVENT_ERROR,
                {"error": MSG_INTERNAL_ERROR, "errorType": ErrorType.INTERNAL.value},
            )
            return

        # A7 通道 1 收口：分类+重分类统一走 classifyAndRecall 单入口。
        # 首帧 META 必须携带无状态分类结果（SSE 事件序列不变），故第一遍以
        # sessionId=None 纯分类；第二遍带 sessionId 完成「加载状态→重分类」。
        firstPass = await self.classifyAndRecall(session, dto.question, needRecall=False)
        result = firstPass.intentResult
        yield StreamEvent(EVENT_META, {"intent": result.intent.value})

        if result.intent == IntentType.CHITCHAT:
            async for event in self._streamChitchat(dto, session):
                yield event
            return

        try:
            classified = await self.classifyAndRecall(
                session, dto.question, sessionId=dto.sessionId, needRecall=False
            )
            state = classified.state
            if state is not None:
                result = classified.intentResult
                yield StreamEvent(EVENT_META, {"intent": result.intent.value})
                if result.intent == IntentType.CHITCHAT:
                    # 重分类后仍可能收敛为闲聊（如问候中含指代词）：同样短路
                    async for event in self._streamChitchat(dto, session):
                        yield event
                    return
            if result.intent in (IntentType.DEFINE, IntentType.MAP, IntentType.METRIC):
                async for event in self._streamDomainCommand(dto, session, result):
                    yield event
                return
            if result.intent == IntentType.CLARIFY:
                async for event in self._streamClarify(dto, session):
                    yield event
                return
            # #207 审查 HIGH 修复：拦截类意图在流式路径同样路由（token + done 卡片对象），
            # 覆盖 SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING / AGENT_RUN 四个卡片意图。
            if result.intent in (
                IntentType.SUPPLIER_360,
                IntentType.SUPPLIER_RISK,
                IntentType.GRAPH_REASONING,
                IntentType.AGENT_RUN,
            ):
                async for event in self._streamInterceptCard(dto, session, result, user):
                    yield event
                return
            _stream_t0 = time.monotonic()
            async for event in self._streamQuery(
                dto, session, result.intent, state, result.chartType,
                suggestion=result.suggested_agent, _t0=_stream_t0,
                semanticState=result.semanticState,  # B5 HIGH-1
            ):
                yield event
        except LlmClientError as exc:
            logger.warning("流式查询 LLM 失败: %s", exc.message)
            await self._storeSessionMessages(session, dto.sessionId, dto.question, exc.message, None)
            yield StreamEvent(EVENT_ERROR, {"error": exc.message, "errorType": ErrorType.LLM.value})
        except DomainError as exc:
            logger.warning("流式查询失败: %s", exc.message)
            await self._storeSessionMessages(session, dto.sessionId, dto.question, exc.message, None)
            if isinstance(exc, Nl2SqlError) and exc.lastPlan is not None:
                yield StreamEvent(EVENT_PLAN, {"plan": exc.lastPlan.to_dict()})
            yield StreamEvent(
                EVENT_ERROR,
                {"error": exc.message, "errorType": ErrorType.DOMAIN.value, "detail": exc.detail},
            )
        except Exception as exc:
            # 未预期异常（DB 连接中断 / 数据解析错误等）：记录完整上下文并产出结构化
            # 错误事件，保证 SSE 连接始终以 error 事件结束而不是被强行中断。
            logger.exception("流式查询未预期错误: %s", exc)
            await self._storeSessionMessages(session, dto.sessionId, dto.question, MSG_INTERNAL_ERROR, None)
            yield StreamEvent(EVENT_ERROR, {"error": MSG_INTERNAL_ERROR, "errorType": ErrorType.INTERNAL.value})

    async def _streamChitchat(
        self, dto: ChatRequest, session: AsyncSession
    ) -> AsyncIterator[StreamEvent]:
        """闲聊流式事件序列（4-4）：先持久化本轮 user+assistant，再产出 token + done。"""
        await self._storeSessionMessages(session, dto.sessionId, dto.question, self._chitchatAnswer(), None)
        for event in self._chitchatStreamEvents():
            yield event

    def _chitchatStreamEvents(self) -> Iterator[StreamEvent]:
        """闲聊的 SSE 事件序列：token(问候) + done(零消耗)。"""
        yield StreamEvent(EVENT_TOKEN, {"content": self._chitchatAnswer()})
        yield StreamEvent(EVENT_DONE, {"tokensUsed": 0, "cost": 0.0, "modelName": None})

    async def _streamClarify(
        self, dto: ChatRequest, session: AsyncSession
    ) -> AsyncIterator[StreamEvent]:
        """CLARIFY 的 SSE 形式：整段概念解释，token 事件 + done（不涉及 SQL）。"""
        pc = await self._buildPipelineContext(
            session, dto, needFewShot=False, needSamples=False, needDrift=False,
        )
        resp = await self._handleClarify(session, dto, pc)
        affinityPayload = (
            {"lockedModel": resp.affinityStatus.lockedModel,
             "remainingTurns": resp.affinityStatus.remainingTurns}
            if resp.affinityStatus is not None
            else None
        )
        yield StreamEvent(EVENT_TOKEN, {"content": resp.answer})
        yield StreamEvent(
            EVENT_DONE,
            {
                "tokensUsed": resp.tokensUsed,
                "cost": resp.cost,
                "modelName": resp.modelName,
                "affinityStatus": affinityPayload,
            },
        )

    async def _streamInterceptCard(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        result: IntentResult,
        user: CurrentUser | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """拦截类意图（360°/风险/图推理/Agent 运行）的 SSE 形式。

        #207 审查 HIGH 修复：此前流式路径从不路由这些意图，默认 streaming UI 下
        卡片（Supplier360Card / SupplierRiskCard / GraphTraversalCard / AgentResponseCard）
        从未渲染。此处复用非流式 handler 得到 ChatResponse（保证两条路径语义一致），
        再转成 token(整段 answer) + done(携带卡片对象)。

        卡片对象序列化必须用 model_dump(by_alias=True)：toSse 的 default=str 会把
        Pydantic 模型直接变成 repr 字符串（见 stream_events.StreamEvent.toSse）。
        """
        if result.intent == IntentType.SUPPLIER_360:
            resp = await self._handleSupplier360(session, dto, result)
        elif result.intent == IntentType.SUPPLIER_RISK:
            resp = await self._handleSupplierRisk(session, dto, result)
        elif result.intent == IntentType.GRAPH_REASONING:
            resp = await self._handleGraphReasoning(session, dto, result)
        elif result.intent == IntentType.AGENT_RUN:
            resp = await self._handleAgentRun(session, dto, result, user=user)
        else:
            # 仅拦截意图可达；其他意图由调用方前置守卫（processMessageStream 拦截）。
            raise AssertionError(f"unexpected intercept intent: {result.intent}")

        yield StreamEvent(EVENT_TOKEN, {"content": resp.answer})
        yield StreamEvent(
            EVENT_DONE,
            {
                "tokensUsed": resp.tokensUsed,
                "cost": float(resp.cost),
                "modelName": resp.modelName,
                "agentRun": (
                    resp.agent_run.model_dump(mode="json", by_alias=True)
                    if resp.agent_run is not None
                    else None
                ),
                "supplier360": (
                    resp.supplier360.model_dump(mode="json", by_alias=True)
                    if resp.supplier360 is not None
                    else None
                ),
                "supplierRisk": (
                    resp.supplier_risk.model_dump(mode="json", by_alias=True)
                    if resp.supplier_risk is not None
                    else None
                ),
                "graphTraversal": (
                    resp.graph_traversal.model_dump(mode="json", by_alias=True)
                    if resp.graph_traversal is not None
                    else None
                ),
            },
        )

    async def _streamQuery(
        self, dto: ChatRequest, session: AsyncSession, intent: IntentType, state: SessionQueryState | None,
        intentChartType: ChartType | None = None,
        suggestion: AgentSuggestion | None = None,
        semanticState: SemanticState | None = None,
        *,
        _t0: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """查询意图的流式流水线：plan → sql → chart → token×N → done（含持久化与状态保存）。

        suggestion（Phase 7 G4）：中置信语义路由命中的建议卡片，随 done 帧透传；
        前端按字段存在性渲染 SuggestedAgentCard。
        _t0：可选的流式计时起点（由调用方传入；不传则从本函数开始计时）。
        semanticState（B5 HIGH-1）：A7 通道 1 抽取的语义快照，用于多步追问链路。
        """
        _stream_t0 = _t0 if _t0 is not None else time.monotonic()
        pc = await self._buildPipelineContext(
            session, dto,
            needFewShot=intent != IntentType.CLARIFY,
            needSamples=intent != IntentType.CLARIFY,
            needDrift=intent != IntentType.CLARIFY,
        )
        # 4-1（feat-token-cache）：cache hit multiplier 入口一次性读，透传到
        # 整条流式流水线的 _costFor / _costForSql 调用。与 chat_service 同口径。
        cacheHitMultiplier = await _readFloatConfig(
            session, "LLM_CACHE_HIT_MULTIPLIER", 0.0,
        )
        # 类召回诊断（2026-09-16）：单步/多步共用此 pc，事件一次性下发；
        # 前端在 truncated/fallback 时向用户提示（静默缺表是可见性盲区）
        if pc.recall is not None:
            yield StreamEvent(
                EVENT_CLASS_RECALL,
                pc.recall.model_dump(mode="json", by_alias=True),
            )
        # B5 HIGH-1：提前计算继承快照（L1/L1.5/B/C 所有多步分支出口共需）。
        prior_snapshot: dict | None = None
        if state is not None:
            prior_snapshot = (
                getattr(state, "inheritance_snapshot", None) or
                ({"inherited_time": None} if state.last_plan else None)
            )
        inherited = self._resolveInheritedState(
            semanticState=semanticState,
            priorSnapshot=prior_snapshot,
            question=dto.question,
        )
        # L1 多步：单步优先策略——明确要求分步 → 直接多步；其余先单步，
        # SQL 执行失败时回退多步拆解（与 processMessage 同口径）。
        if intent in (IntentType.NEW_QUERY, IntentType.QUERY):
            if self._stepPlanner.is_explicit_multi_step(dto.question):
                global_filters = await self._resolveGlobalFilters(session, dto, pc)
                dto, multi_plan, pc, step_tokens, step_cost = await self._resolveExplicitMultiStep(
                    session, dto, pc,
                )
                if multi_plan is not None:
                    async for event in self._streamMultiStep(
                        dto, session, pc, multi_plan, state,
                        initial_tokens=step_tokens, initial_cost=step_cost,
                        suggestion=suggestion, _t0=_stream_t0,
                        global_filters=global_filters,
                        semanticState=inherited,
                        priorSnapshot=prior_snapshot,
                    ):
                        yield event
                    return
            # L1.5（2026-08-17 真实回归）：并列复合问题启发式触发拆步前置。
            # 与 processMessage 同口径；详见 _looks_like_compound_question。
            elif _looks_like_compound_question(dto.question):
                global_filters = await self._resolveGlobalFilters(session, dto, pc)
                dto, multi_plan, pc, step_tokens, step_cost = await self._resolveExplicitMultiStep(
                    session, dto, pc,
                )
                if multi_plan is not None:
                    async for event in self._streamMultiStep(
                        dto, session, pc, multi_plan, state,
                        initial_tokens=step_tokens, initial_cost=step_cost,
                        suggestion=suggestion, _t0=_stream_t0,
                        global_filters=global_filters,
                        semanticState=inherited,
                        priorSnapshot=prior_snapshot,
                    ):
                        yield event
                    return
        # B（feat-follow-up-cascade）：FOLLOW_UP 且上一轮是多步 → 改写 + 多步重跑
        # （失败退回单轮状态注入，与 _handleGenericQuery 同口径）。
        if intent == IntentType.FOLLOW_UP and state is not None:
            prepared = await self._prepareFollowUpMultiStep(session, dto, pc, state)
            if prepared is not None:
                dto2, multiPlan, msTokens, msCost, gf2 = prepared
                async for event in self._streamMultiStep(
                    dto2, session, pc, multiPlan, state,
                    initial_tokens=msTokens, initial_cost=msCost,
                    suggestion=suggestion, _t0=_stream_t0,
                    global_filters=gf2,
                    semanticState=inherited,
                    priorSnapshot=prior_snapshot,
                ):
                    yield event
                return
        outcome = await self._planAndGenerateSql(session, dto, pc, intent, state)
        if outcome.sql is None and self._isFollowUpRetryCandidate(dto.question, intent, state):
            # C：短句新查询计划不可回答 → 升级追问重试一次（先 B 多步重跑，再退回
            # 单轮 FOLLOW_UP 状态注入；仍不可回答则走下方固定兜底文案）。
            logger.info("不可回答短句升级追问重试: %s", dto.question)
            # _isFollowUpRetryCandidate 已保证 state 非空，此处可直接传入
            prepared = await self._prepareFollowUpMultiStep(session, dto, pc, state)
            if prepared is not None:
                dto2, multiPlan, msTokens, msCost, gf2 = prepared
                async for event in self._streamMultiStep(
                    dto2, session, pc, multiPlan, state,
                    initial_tokens=msTokens, initial_cost=msCost,
                    suggestion=suggestion, _t0=_stream_t0,
                    global_filters=gf2,
                    semanticState=inherited,
                    priorSnapshot=prior_snapshot,
                ):
                    yield event
                return
            outcome = await self._planAndGenerateSql(
                session, dto, pc, IntentType.FOLLOW_UP, state
            )
            if outcome.sql is not None:
                intent = IntentType.FOLLOW_UP
        if outcome.sql is None:
            # 计划 target=无法回答：plan + 固定友好回答 + done，不生成 sql/chart，不执行查询
            answer = self._unanswerableAnswerText(dto.question, pc.classes)
            # 单步执行计划：统一展示 MultiStepPlanCard（2026-08-16）
            yield self._singleStepOverview("无法回答", dto.question)
            yield self._singleStepStart("无法回答", dto.question)
            if outcome.plan is not None:
                yield StreamEvent(EVENT_PLAN, {"plan": outcome.plan.to_dict()})
            yield StreamEvent(EVENT_TOKEN, {"content": answer})
            yield self._stepResultEvent(StepResult(
                step_index=0,
                description="无法回答",
                sub_question=dto.question,
                sql=None,
                data=None,
                summary="该问题当前数据条件下无法回答",
            ))
            await self._storeSessionMessages(
                session, dto.sessionId, dto.question, answer, None,
                routing_layer=ROUTING_LAYER_L2,
                latency_ms=int((time.monotonic() - _stream_t0) * 1000),
                token_cost_usd=float(self._costForSql(outcome, pc.selected, cacheHitMultiplier=cacheHitMultiplier)),
            )
            await self._saveQueryState(
                session, dto.sessionId,
                question=dto.question, plan=outcome.plan, sql=None, resultColumns=[],
            )
            totalTokens = outcome.promptTokens + outcome.completionTokens + outcome.wasted[0] + outcome.wasted[1]
            totalCost = self._costForSql(outcome, pc.selected, cacheHitMultiplier=cacheHitMultiplier)
            affinityConfig = outcome.sqlConfig or pc.selected
            affinity = await self._buildAffinityStatus(
                session, dto.sessionId, affinityConfig.id, affinityConfig.model_name,
            )
            affinityPayload = (
                {"lockedModel": affinity.lockedModel, "remainingTurns": affinity.remainingTurns}
                if affinity is not None
                else None
            )
            yield StreamEvent(
                EVENT_DONE,
                {
                    "tokensUsed": totalTokens,
                    "cost": float(totalCost),
                    # 计划由实际服务模型（可能为降级后的 fallback）生成，如实上报
                    "modelName": affinityConfig.model_name,
                    "latency_ms": int((time.monotonic() - _stream_t0) * 1000),
                    "affinityStatus": affinityPayload,
                    "suggestedAgent": (
                        suggestion.model_dump(mode="json", by_alias=True)
                        if suggestion is not None
                        else None
                    ),
                },
            )
            return
        totalTokens = outcome.promptTokens + outcome.completionTokens + outcome.wasted[0] + outcome.wasted[1]
        totalCost = self._costForSql(outcome, pc.selected, cacheHitMultiplier=cacheHitMultiplier)
        # 单步执行计划：统一展示 MultiStepPlanCard（2026-08-16）。description 取计划
        # target（ReAct 阶段产出的核心实体），plan 为 None 时退化为问题截断。
        stepDesc = _clipText(
            outcome.plan.target if outcome.plan else dto.question, limit=20,
        )
        yield self._singleStepOverview(stepDesc, dto.question)
        yield self._singleStepStart(stepDesc, dto.question)
        # ReAct 查询计划（Phase E）：REFINE 捷径命中时 plan 为上一轮计划，照常下发
        if outcome.plan is not None:
            yield StreamEvent(EVENT_PLAN, {"plan": outcome.plan.to_dict()})
        yield StreamEvent(EVENT_SQL, {"sql": outcome.sql})

        # Phase 4.4：检测计划是否引用了可用 Feature，命中则直接回流特征值（SSE 流）。
        featureResp = await self._tryFeatureResponse(session, dto, pc, outcome)
        if featureResp is not None:
            await self._storeSessionMessages(session, dto.sessionId, dto.question, featureResp.answer, None)
            await self._saveQueryState(
                session, dto.sessionId,
                question=dto.question, plan=outcome.plan, sql=None, resultColumns=[],
            )
            affinity = await self._buildAffinityStatus(
                session, dto.sessionId, pc.selected.id, pc.selected.model_name,
            )
            affinityPayload = (
                {"lockedModel": affinity.lockedModel, "remainingTurns": affinity.remainingTurns}
                if affinity is not None
                else None
            )
            # 从非流式 ChatResponse 提取 steps 渲染 SSE step result 事件
            for step in (featureResp.steps or []):
                yield self._stepResultEvent(StepResult(
                    step_index=step.step_index,
                    description=step.description,
                    sub_question=step.sub_question or dto.question,
                    sql=None,
                    data=step.data,
                    summary=step.summary,
                    chart_type=step.chart_type,
                    chart_option=step.chart_option,
                ))
            yield StreamEvent(EVENT_TOKEN, {"content": featureResp.answer})
            yield StreamEvent(
                EVENT_DONE,
                {
                    "tokensUsed": featureResp.tokensUsed,
                    "cost": featureResp.cost,
                    "modelName": featureResp.modelName,
                    "affinityStatus": affinityPayload,
                    "suggestedAgent": (
                        suggestion.model_dump(mode="json", by_alias=True)
                        if suggestion is not None
                        else None
                    ),
                },
            )
            return

        try:
            data, finalSql, retryTokens = await self._runQueryWithRetry(session, dto, pc, outcome)
        except Exception as exc:
            # 双失败时「重试生成」的 token 随异常交回：无论随后回退多步还是原样上抛都
            # 必须落账。流式尤其关键：上抛会被收敛成 error 事件后正常收尾（HTTP 200、
            # 事务提交），漏记就是永久缺口（核心约束 #3——失败路径也是计量路径）
            retryUsage = await self._accountRetryGenUsage(session, dto, exc, pc, outcome)
            # 单步执行失败：回退多步拆解（可拆出 ≥2 数据步时走多步；否则重抛原错误）
            if intent in (IntentType.NEW_QUERY, IntentType.QUERY):
                detected = await self._detectMultiStep(session, dto, pc)
                if detected is not None and detected.plan is not None:
                    # 单步已消耗的生成 token/成本 + 拆步判定消耗（+ 重试生成，见上）
                    # 一并计入多步响应总额
                    prior_tokens = (
                        outcome.promptTokens + outcome.completionTokens
                        + outcome.wasted[0] + outcome.wasted[1]
                        + detected.prompt_tokens + detected.completion_tokens
                    )
                    prior_cost = self._costForSql(outcome, pc.selected, cacheHitMultiplier=cacheHitMultiplier) + self._costFor(
                        pc.selected, detected.prompt_tokens, detected.completion_tokens,
                        cacheHitMultiplier=cacheHitMultiplier,
                    )
                    if retryUsage is not None:
                        prior_tokens += retryUsage.tokens
                        prior_cost += retryUsage.cost
                    async for event in self._streamMultiStep(
                        dto, session, pc, detected.plan, state,
                        initial_tokens=prior_tokens, initial_cost=prior_cost,
                        suggestion=suggestion,
                    ):
                        yield event
                    return
            raise
        if finalSql != outcome.sql:
            # 执行错误回灌重试后 SQL 已修正：补发更正后的 SQL 事件（1-3）
            yield StreamEvent(EVENT_SQL, {"sql": finalSql})
        self._spawnEmbedding(dto, finalSql)
        # 执行错误回灌重试额外消耗计入总量并审计（1-3）
        if retryTokens[0] or retryTokens[1]:
            retryCfg = outcome.sqlConfig or pc.selected
            totalTokens += retryTokens[0] + retryTokens[1]
            totalCost += self._costFor(retryCfg, retryTokens[0], retryTokens[1], cacheHitMultiplier=cacheHitMultiplier)
            await self._recordUsage(
                session, dto.sessionId, retryCfg,
                retryTokens[0], retryTokens[1], purpose=USAGE_PURPOSE_NL2SQL,
            )

        chartType, option, tableOption, rationale, chartPt, chartCt, chartCached = await self._chartStep(
            session, dto, pc, data, intentChartType, outcome.plan
        )
        totalTokens += chartPt + chartCt
        # 4-2（feat-token-cache 续）：chart 阶段 cachedTokens 透传到流式汇总的
        # _costFor 调用，与非流式路径同口径。multiplier 由 _streamQuery 入口
        # 一次性读 _LLM_CACHE_HIT_MULTIPLIER，沿用 cacheHitMultiplier 局部变量。
        totalCost += self._costFor(
            pc.selected, chartPt, chartCt,
            cachedTokens=chartCached, cacheHitMultiplier=cacheHitMultiplier,
        )
        yield StreamEvent(EVENT_CHART, {
            "chartType": chartType.value,
            "chartOption": option,
            "tableOption": tableOption,
            "visualRationale": rationale,
            "data": data,
        })

        # Phase 1.4：拉取目标表的可信度 badge 并通过 SSE 单独下发（前端订阅后渲染）
        # 在 chart 之后、answer 流之前：不影响用户感知的回答延迟；DQ 故障由 helper 内部静默
        dqBadges = await self._buildDataQualityBadges(session, outcome)
        if dqBadges is not None:
            yield StreamEvent(
                EVENT_DATA_QUALITY,
                {"badges": [b.model_dump(mode="json", by_alias=True) for b in dqBadges]},
            )

        # 回答流式输出（失败降级：仅当主模型未产出任何 token 时）
        answerPieces: list[str] = []
        # H4 断连兜底：把将要在 yield 之前产生的内容登记到快照上（此后客户端看到的
        # 任何片段都已在快照里）。answerPieces 是同一个列表引用 ⇒ 追加即对兜底可见。
        persistState = streamPersistStateOf(session)
        if persistState is not None:
            persistState.answerPieces = answerPieces
            persistState.sql = finalSql
            persistState.plan = outcome.plan
            persistState.resultColumns = self._columns(data)
            persistState.totalCostUsd = float(totalCost)
        # L2 歧义示警（feat-nl2sql-share-denominator-guard）：全组占比恒 100% 不可
        # 数学判错 → 流式下发的首个 token 即提示（快照 answerPieces 同引用，先追加
        # 后 yield 保证断连兜底可见）。violations 已在 _runQueryWithRetry 出口抛错。
        shareWarning = shareAmbiguityWarning(outcome.plan, data)
        if shareWarning:
            answerPieces.append(shareWarning)
            yield StreamEvent(EVENT_TOKEN, {"content": shareWarning})
        # 默认取主模型名：即使流异常地零块完成，done 事件仍报告一个合理的模型名
        answerModelName: str | None = pc.selected.model_name
        # Think_Hide（feat-think-hide）：开启时对 token 流增量过滤 <think> 思维链，
        # answerPieces 只收过滤后的内容（断连兜底落库与用户所见一致）
        thinkFilter: ThinkStreamFilter | None = (
            ThinkStreamFilter() if await isThinkHideEnabled(session) else None
        )
        async for chunk, answerConfig, (wastedPt, wastedCt) in self._streamAnswerWithFallback(
            session, dto.sessionId, pc.configs, pc.selected, dto, finalSql, data,
            forced=pc.forcedModel, history=pc.contextPrompt,
        ):
            answerModelName = answerConfig.model_name
            if chunk.isDone:
                totalTokens += chunk.promptTokens + chunk.completionTokens + wastedPt + wastedCt
                totalCost += self._costFor(
                    answerConfig, chunk.promptTokens, chunk.completionTokens,
                    cacheHitMultiplier=cacheHitMultiplier,
                )
                # wasted 是降级前主模型已消耗 token（Nl2SqlError.tokens 或 0），
                # 无成功响应 → 不带 cachedTokens，保持 None（与 chat_usage 同口径）。
                totalCost += self._costFor(
                    pc.selected, wastedPt, wastedCt, cacheHitMultiplier=cacheHitMultiplier,
                )
                await self._recordUsage(
                    session, dto.sessionId, answerConfig,
                    chunk.promptTokens, chunk.completionTokens, purpose=USAGE_PURPOSE_ANSWER,
                )
                if persistState is not None:
                    persistState.totalCostUsd = float(totalCost)
            if chunk.content:
                # 独立 if 而非 elif：即使 isDone 块携带内容也不丢失
                content = thinkFilter.feed(chunk.content) if thinkFilter else chunk.content
                if content:
                    answerPieces.append(content)
                    yield StreamEvent(EVENT_TOKEN, {"content": content})
        if thinkFilter is not None:
            # 流收尾：NORMAL 态吐出截断的候选缓冲，IN_THINK 态为空（未闭合=隐藏）
            tail = thinkFilter.flush()
            if tail:
                answerPieces.append(tail)
                yield StreamEvent(EVENT_TOKEN, {"content": tail})

        answer = "".join(answerPieces)
        # L2 streaming: totalCost includes SQL + chart + answer LLM costs
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, answer, finalSql,
            routing_layer=ROUTING_LAYER_L2,
            latency_ms=int((time.monotonic() - _stream_t0) * 1000),
            token_cost_usd=float(totalCost),
            # 0105：单步流的图进「最终报告」（导出 PDF / 历史回放）。与流式下发的
            # 那份是同一份 —— 图不能只活在实时响应里。
            chart_type=chartType,
            chart_option=option,
            # 0107：图之外的明细表 + 判断依据同轮落库（与流式下发那份同一份）。
            table_option=tableOption,
            visual_rationale=rationale,
        )
        await self._saveQueryState(
            session, dto.sessionId,
            question=dto.question, plan=outcome.plan, sql=finalSql,
            resultColumns=self._columns(data),
        )
        # affinity 需用 answerConfig（实际服务的回答模型），与 PC.selected 解耦
        answerConfigId = (await self._tokenUsage.getLastModelId(session, dto.sessionId)) or pc.selected.id
        # 仅在已锁定（同上一轮模型）时返回 status
        affinityTurns = self._resolveAffinityTurns()
        turnCount = await self._tokenUsage.getSessionTurnCount(session, dto.sessionId)
        affinityPayload: dict | None = None
        if (
            await self._tokenUsage.getLastModelId(session, dto.sessionId) is not None
            and turnCount < affinityTurns + 1  # 已记录本轮后 turnCount 比"前 N 轮"判断多 1
        ):
            # 简化：调用 _buildAffinityStatus 以走完整逻辑（含 lastModelId == currentModelId 校验）
            affinity = await self._buildAffinityStatus(
                session, dto.sessionId, answerConfigId, answerModelName or "",
            )
            if affinity is not None:
                affinityPayload = {
                    "lockedModel": affinity.lockedModel,
                    "remainingTurns": affinity.remainingTurns,
                }
        # 单步执行计划收尾：把执行结果（含 SQL/数据/摘要）下发给前端卡片（2026-08-16）
        yield self._stepResultEvent(StepResult(
            step_index=0,
            description=stepDesc,
            sub_question=dto.question,
            sql=finalSql,
            data=data,
            summary=self._summarizeStepData(data),
            table_option=tableOption,
            visual_rationale=rationale,
        ))
        # v3.1 B6（M7）：流式假设后处理——只落库，不进 SSE 帧（前端靠 GET 端点取）
        await self._maybeGenerateHypotheses(
            session, dto.sessionId, dto.question, pc, data=data,
        )
        yield StreamEvent(
            EVENT_DONE,
            {
                "tokensUsed": totalTokens,
                "cost": float(totalCost),
                "modelName": answerModelName,
                "latency_ms": int((time.monotonic() - _stream_t0) * 1000),
                "affinityStatus": affinityPayload,
                "suggestedAgent": (
                    suggestion.model_dump(mode="json", by_alias=True)
                    if suggestion is not None
                    else None
                ),
            },
        )

    async def _streamMultiStep(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        pc: _PipelineContext,
        multiStepPlan: MultiStepPlan,
        state: SessionQueryState | None,
        *,
        initial_tokens: int = 0,
        initial_cost: Decimal = Decimal("0"),
        suggestion: AgentSuggestion | None = None,
        _t0: float | None = None,
        global_filters: GlobalFilters | None = None,
        semanticState: InheritedState | None = None,
        priorSnapshot: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """多步查询的流式事件序列：step_plan/step_result × N → token(汇总) → done。

        与 _executeMultiStep（非流式）同构，但逐步 yield 中间步骤事件，前端可
        实时渲染每一步的 SQL 与数据。每步复用 _planAndGenerateSql（两阶段 + 重试
        降级），步骤失败记录 error 不阻断后续步骤。

        initial_tokens/initial_cost：进入多步前已消耗的 token/成本（拆步判定、或
        单步失败回退时已消耗的单步生成），计入 done 事件的 tokensUsed/cost。

        suggestion（Phase 7 G4）：中置信语义路由建议卡片随 done 帧透传，
        与 _streamQuery 的 done 帧口径一致（G4 审查 MEDIUM 修复）。

        semanticState / priorSnapshot（B5 HIGH-1 修复）：多步 B/C 路径的追问改写
        重跑链路，与非流式 _executeMultiStep 同口径。

        _t0：可选的流式计时起点（由调用方传入；不传则从本函数开始计时）。

        global_filters（feat-multistep-global-filter B 层）：跨步骤共享的范围类
        约束，与非流式 _executeMultiStep 同口径注入 StepExecutionContext；调用方
        经 _resolveGlobalFilters 抽取后透传（C1/C2 修复：此前流式路径缺此形参，
        导致追问多步抛 TypeError 且显式/复合多步从不注入全局约束）。
        """
        _ms_t0 = _t0 if _t0 is not None else time.monotonic()
        if self._isOversizedPlan(multiStepPlan):
            # 与非流式 _executeMultiStep 同口径，共用 _rejectOversizedPlan（落库 +
            # 存状态），两条路径不会各写一份文案。放在读 cache multiplier 之前：
            # 拒收不需要它，省一次 DB 读。
            #
            # 事件序列照抄本文件「计划 target=无法回答」分支（同为「不给数据步、
            # 只给固定回答」）：单步概览 + 起始 → token → step_result 收尾。
            # step_result 不能省——概览已把它下发为「待执行」，不收尾则前端那张
            # 卡片永远转圈（见 TestStepFailureIsolation 的汇总步同款教训）。
            answer = await self._rejectOversizedPlan(
                session, dto, multiStepPlan,
                total_cost=initial_cost, _t0=_ms_t0,
            )
            yield self._singleStepOverview("超出步数上限", dto.question)
            yield self._singleStepStart("超出步数上限", dto.question)
            yield StreamEvent(EVENT_TOKEN, {"content": answer})
            yield self._stepResultEvent(self._oversizedStepResult(dto))
            yield StreamEvent(
                EVENT_DONE,
                {
                    "tokensUsed": initial_tokens,
                    "cost": float(initial_cost),
                    "modelName": None,
                    "latency_ms": int((time.monotonic() - _ms_t0) * 1000),
                    "affinityStatus": None,
                    "suggestedAgent": (
                        suggestion.model_dump(mode="json", by_alias=True)
                        if suggestion is not None
                        else None
                    ),
                },
            )
            return
        # 4-1（feat-token-cache）：与 _streamQuery 同口径，入口一次性读 multiplier。
        cacheHitMultiplier = await _readFloatConfig(
            session, "LLM_CACHE_HIT_MULTIPLIER", 0.0,
        )
        ctx = StepExecutionContext(
            datasource_type=pc.ds.type,
            oracle_version=pc.ds.oracle_version,
            schema_prefix=pc.ds.username,
            context=pc.contextPrompt,
            global_filters=global_filters,
        )
        completed: list[StepResult] = []
        total_tokens = initial_tokens
        total_cost = initial_cost
        last_model_name: str | None = None
        last_plan: QueryPlan | None = None
        last_sql: str | None = None
        last_data: list[dict] = []

        # 落库只针对**数据步**（汇总步不执行 SQL、没有 sql/data 可落）。
        # 汇总步仍计入 completedCount（见下方 early-return 封口），故 runStatusFor
        # 的分母用 len(multiStepPlan.steps)（含汇总步）而不是 len(subQuestions)。
        # `totalSteps` 也传这同一个数（IMP-7）：`total_steps` / `completed_steps` /
        # 收尾哨兵 `current_step_idx` 三者必须同源，否则正常计划落库成 3/2。
        subQuestions = [s.description or s.sub_question for s in multiStepPlan.data_steps]
        # 续跑分支 + 新建分支的唯一实现（与非流式共用同一 mixin 方法，见 (c) 裁决）。
        run, startIndex = await self._beginRunForRequest(
            session, dto,
            subQuestions=subQuestions,
            totalSteps=len(multiStepPlan.steps),
        )
        persisted = await persistence.loadSteps(session, run.id) if run is not None else []
        stepsByIdx = {s.step_index: s for s in persisted}
        # 计数器**绝不能**复用上面那个 completed 列表（list[StepResult]，是
        # _hasDataStepResult / done 帧 steps 的入参）；这里一律用 int。
        completedCount = 0
        anyFailed = False
        anySkipped = False

        # 循环前一次性下发完整计划概览，前端据此渲染各步骤的「待执行」状态。
        # **必须在下发之前**取得 run：顺序反了 run 还是 None，runId 永远发不出去。
        yield StreamEvent(EVENT_MULTI_STEP_PLAN, {
            # Task 9：前端凭 runId 调 POST /chat/multi-step/{runId}/resume。
            # 单步路径（_singleStepOverview）不落库、没有 run，故意不带这个键；
            # 前端必须按「可选」处理，缺省时不渲染续跑按钮。
            "runId": str(run.id) if run is not None else None,
            # F7/IMP-6：续跑时 `index < startIndex` 的步不执行（循环里只 continue、
            # 不发事件）。概览必须回放它们已持久化的终态，否则会从「已完成」退回
            # 「待执行」（见 _overviewStepPayload）。
            "steps": [
                _overviewStepPayload(s, stepsByIdx, startIndex)
                for s in multiStepPlan.steps
            ],
        })

        for index, step_plan in enumerate(multiStepPlan.steps):
            if await self._shouldSkipStep(index, startIndex):
                # 续跑：更早的步已 succeeded。跳过执行但**照样计入完成数**（理由见
                # `_shouldSkipStep` 的文档字符串）。
                completedCount += 1
                continue
            if step_plan.aggregation_only:
                if not _hasDataStepResult(completed):
                    # 与非流式同判据（C3）：只有错误行时不调汇总 LLM，直接降级收尾
                    logger.warning("多步数据步骤全部失败，跳过汇总步骤")
                    # 计划概览已把汇总步下发给前端（初始「待执行」），跳过时必须补一个终态事件，
                    # 否则该步永远停在「待执行」——恰发生在用户最需要看清失败原因的场景。
                    # 只发事件、不进 completed：末帧 steps 与非流式一致地只含数据步骤，
                    # 也避免把汇总步自己算进 `_finalizeMultiStepDegrade` 的「完成 N/M 步」分母。
                    yield self._stepResultEvent(_failedStepResult(
                        step_plan, _MSG_STEP_AGGREGATION_SKIPPED,
                    ))
                    continue
                # 汇总步骤开始前也发 step_plan，使「当前执行步骤」覆盖到汇总对比
                yield StreamEvent(EVENT_STEP_PLAN, {
                    "stepIndex": step_plan.index,
                    "description": step_plan.description,
                    "subQuestion": step_plan.sub_question,
                })
                # Task 2：await 不能写进 lambda，阈值在 lambda 外先算好再捕获
                # （与非流式 _executeMultiStep 同口径，两条路径同源不漂移）。
                full_data_threshold = await loadFullDataThreshold(session)
                agg_resp = await self._callWithFallback(
                    session, dto.sessionId, pc.configs, pc.selected, "answer",
                    lambda cfg: self._stepAggregator.aggregate(
                        dto.question, multiStepPlan, completed,
                        self._llmFactory(cfg), cfg.model_name,
                        history=pc.contextPrompt,
                        full_data_threshold=full_data_threshold,
                    ),
                    forced=pc.forcedModel,
                )
                # Think_Hide（feat-think-hide）：汇总答案按系统参数剥离 <think> 思维链
                agg_content = await applyThinkPolicy(session, agg_resp[0].content)
                agg_config = agg_resp[1]
                agg_pt = agg_resp[0].promptTokens
                agg_ct = agg_resp[0].completionTokens
                agg_cached = getattr(agg_resp[0], "cachedTokens", None)
                wasted_pt, wasted_ct = agg_resp[2]
                total_tokens += agg_pt + agg_ct + wasted_pt + wasted_ct
                # 4-2（feat-token-cache 续）：多步聚合 answer cachedTokens 透传，
                # 与单步 _summarizeUsage 同口径。
                total_cost += self._costFor(
                    agg_config, agg_pt, agg_ct,
                    cachedTokens=agg_cached, cacheHitMultiplier=cacheHitMultiplier,
                )
                total_cost += self._costFor(
                    pc.selected, wasted_pt, wasted_ct,
                    cacheHitMultiplier=cacheHitMultiplier,
                )
                last_model_name = agg_config.model_name
                await self._recordUsage(
                    session, dto.sessionId, agg_config,
                    agg_pt, agg_ct, purpose=USAGE_PURPOSE_ANSWER,
                )
                await self._storeSessionMessages(
                    session, dto.sessionId, dto.question, agg_content, None,
                    routing_layer=ROUTING_LAYER_L2,
                    latency_ms=int((time.monotonic() - _ms_t0) * 1000),
                    token_cost_usd=float(total_cost),
                    # 汇总步是纯文字、无图；每步的图已在各自 steps 里落库
                    # （0107 补 rationale：顶层不附图，SUMMARY_TEXT_ONLY 解释为什么）。
                    chart_type=None,
                    chart_option=None,
                    table_option=None,
                    visual_rationale=summaryTextOnlyRationale().to_dict(),
                )
                await self._saveQueryState(
                    session, dto.sessionId,
                    question=dto.question, plan=last_plan, sql=last_sql,
                    resultColumns=self._columns(last_data),
                    inheritance_snapshot=priorSnapshot,  # B5 HIGH-1：传递用于下一轮追问
                )
                affinity = await self._buildAffinityStatus(
                    session, dto.sessionId, agg_config.id, agg_config.model_name,
                )
                affinity_payload = (
                    {"lockedModel": affinity.lockedModel,
                     "remainingTurns": affinity.remainingTurns}
                    if affinity is not None
                    else None
                )
                yield StreamEvent(EVENT_TOKEN, {"content": agg_content})
                # v3.1 B6（M7）：流式多步假设后处理——只落库，不进 SSE 帧
                await self._maybeGenerateHypotheses(
                    session, dto.sessionId, dto.question, pc, data=last_data,
                )
                yield StreamEvent(
                    EVENT_DONE,
                    {
                        "tokensUsed": total_tokens,
                        "cost": float(total_cost),
                        "modelName": last_model_name,
                        "latency_ms": int((time.monotonic() - _ms_t0) * 1000),
                        "affinityStatus": affinity_payload,
                        "steps": [_step_result_to_read(s).model_dump(by_alias=True) for s in completed],
                        # 汇总步不发 step_result（纯文字），SUMMARY_TEXT_ONLY 只能经 done 帧抵达前端
                        "visualRationale": summaryTextOnlyRationale().to_dict(),
                        "suggestedAgent": suggestion.model_dump(mode="json", by_alias=True)
                        if suggestion is not None else None,
                    },
                )
                # 本 return 在**循环体内**，走不到循环之后的统一 _closeRun ⇒ 计划含
                # 汇总步时（线上常态）run 会永远停在 running（僵尸），必须在此封口。
                # 汇总步本身也算「跑完了」，不 +1 的话 completedCount 永远 <
                # len(steps)（分母含汇总步）⇒ runStatusFor 把成功的 run 判成 failed。
                completedCount += 1
                # _closeRun 自己就 `if run is None: return`（kill switch 关掉时 run=None），
                # 不需要外面再包一层判断。
                await self._closeRun(
                    session, run,
                    status=runStatusFor(
                        completedCount, len(multiStepPlan.steps), anyFailed, anySkipped
                    ),
                    completedSteps=completedCount,
                    currentStepIdx=len(multiStepPlan.steps),
                )
                await session.commit()
                return

            # 数据查询步骤：先下发计划事件，再执行（与非流式共用 helper），最后下发结果事件
            yield StreamEvent(EVENT_STEP_PLAN, {
                "stepIndex": step_plan.index,
                "description": step_plan.description,
                "subQuestion": step_plan.sub_question,
            })
            # 压缩判定必须发生在**构造本步 prompt 之前**：超阈值时把更早的已成功步
            # 压成 data_compressed，本步注入的是压缩后的那份（本步自己用原始 data）。
            #
            # 压缩会把**更早的**步置为 compressed（其 step_result 早就发过了），
            # 前端无从得知 —— 故这里在调用前后对比 data_compressed，为每个**新**
            # 被压缩的步补发一条 step_compressed（Task 9 的「已压缩」徽章靠它）。
            compressedBefore = {s.step_index: s.data_compressed for s in persisted}
            if run is not None:
                await self._maybeCompressPriorSteps(
                    session, run, persisted, nextStepIdx=index,
                    maxInputTokens=_maxInputTokens(pc),
                    injectionText=ctx.inject_to_prompt(index),
                )
                for s in persisted:
                    compressed = s.data_compressed
                    if compressed is None or compressedBefore.get(s.step_index) is not None:
                        continue
                    meta = compressed.get("meta") or {}
                    yield StreamEvent(EVENT_STEP_COMPRESSED, {
                        "stepIndex": s.step_index,
                        "originalRows": int(meta.get("original_rows") or 0),
                        "compressedRows": int(meta.get("compressed_rows") or 0),
                    })
            if run is not None:
                await persistence.markStepRunning(session, stepsByIdx[index])
            # 瞬态重试：brief Step 4 的「二选一」取**选项 B**（与非流式同口径）——
            # 不在此处包 `runWithTransientRetry`，保留对 `_executeDataStep` 的原样
            # 调用，只在异常分支接 `_persistStepFailure`。依据见
            # chat_multistep._executeMultiStep 的同名注释：helper 内部已有瞬态/回灌
            # 重试，外层重包会让用量重复计账且覆盖不到「返回 error 行」的软失败。
            try:
                stepRun = await self._executeDataStep(session, dto, pc, ctx, step_plan, state)
            except Exception as exc:  # noqa: BLE001 - 步骤级隔离：单步硬失败不阻断后续步
                logger.warning("多步步骤硬失败，隔离该步骤: step=%d", index, exc_info=True)
                if run is not None:
                    await self._recordHardFailure(session, run, stepsByIdx[index], exc)
                    anyFailed = True
                # 失败的步也必须下发终态事件，否则概览里那张卡片永远停在「待执行」
                # ——恰发生在用户最需要看清失败原因的场景（同「跳过汇总」的既有先例）。
                yield self._stepResultEvent(_failedStepResult(
                    step_plan, f"{type(exc).__name__}: {exc}",
                ))
                continue
            total_tokens += stepRun.tokens
            total_cost += stepRun.cost
            if stepRun.modelName:
                last_model_name = stepRun.modelName
            completed.append(stepRun.result)
            if stepRun.result.sql is None:
                # 软失败（LLM 判无法回答 / 执行 + 回灌重试均失败）不是异常：_executeDataStep
                # 已把它收敛为 error 行（sql=None 是失败标记）。终态仍需落 failed
                # （spec §6.3），否则该步永远停在 running。
                #
                # 失败记档走软失败入口（spec §6.1 要求软失败同样落 last_error /
                # last_error_kind / attempt_count；异常本体已丢，只有错误文案）。
                # 用量只记一次 —— finishStep 会**累加** tokens/cost，两处都传就双记。
                if run is not None:
                    await self._recordSoftFailure(
                        session, stepsByIdx[index],
                        message=stepRun.result.error or "", kind=ERROR_KIND_PERMANENT,
                        run=run, tokens=stepRun.tokens, cost=stepRun.cost,
                    )
                    anyFailed = True
            elif run is not None:
                await self._persistStepSuccess(
                    session, stepsByIdx[index],
                    sql=stepRun.result.sql, data=stepRun.result.data,
                    chartOption=stepRun.result.chart_option,
                    modelUsed=stepRun.modelName,
                    tokens=stepRun.tokens, cost=stepRun.cost,
                )
                completedCount += 1
            ctx = ctx.with_step(stepRun.result, chartLabelUsed=stepRun.chart_label_calls > 0)
            if stepRun.result.sql is not None:
                # 只有成功步骤才更新追问锚点（与非流式同口径）
                last_plan = stepRun.plan
                last_sql = stepRun.result.sql
                last_data = stepRun.result.data
            yield self._stepResultEvent(stepRun.result)

        # 计划里没有汇总步（异常形态）时的统一收尾。分母用 len(multiStepPlan.steps)
        # （含汇总步）与汇总分支的封口同口径。
        await self._closeRun(
            session, run,
            status=runStatusFor(
                completedCount, len(multiStepPlan.steps), anyFailed, anySkipped
            ),
            completedSteps=completedCount,
            currentStepIdx=len(multiStepPlan.steps),
        )
        await session.commit()

        # 异常降级：所有步骤都不是 aggregation_only（与非流式共用收尾逻辑）
        degrade_answer = await self._finalizeMultiStepDegrade(
            session, dto, completed,
            last_plan=last_plan, last_sql=last_sql, last_data=last_data,
            total_cost=total_cost, _t0=_ms_t0,
            inheritance_snapshot=priorSnapshot,  # B5 HIGH-1：传递用于下一轮追问
        )
        yield StreamEvent(EVENT_TOKEN, {"content": degrade_answer})
        # v3.1 B6（M7）：降级收尾同样接假设后处理（last_data 为空时静默跳过）
        await self._maybeGenerateHypotheses(
            session, dto.sessionId, dto.question, pc, data=last_data,
        )
        yield StreamEvent(
            EVENT_DONE,
            {
                "tokensUsed": total_tokens,
                "cost": float(total_cost),
                "modelName": last_model_name,
                "latency_ms": int((time.monotonic() - _ms_t0) * 1000),
                "queryPlan": last_plan.to_dict() if last_plan else None,
                # 降级收尾同样是纯文字：SUMMARY_TEXT_ONLY 只能经 done 帧抵达前端
                "visualRationale": summaryTextOnlyRationale().to_dict(),
                "suggestedAgent": suggestion.model_dump(mode="json", by_alias=True)
                if suggestion is not None else None,
            },
        )

    @staticmethod
    def _stepResultEvent(result: StepResult) -> StreamEvent:
        """把 StepResult 转为 EVENT_STEP_RESULT 事件（含数据与该步的图）。"""
        return StreamEvent(EVENT_STEP_RESULT, {
            "stepIndex": result.step_index,
            "description": result.description,
            "subQuestion": result.sub_question,
            "sql": result.sql,
            "data": result.data if result.data else None,
            "summary": result.summary,
            "error": result.error,
            "chartType": result.chart_type,
            "chartOption": result.chart_option,
            "tableOption": result.table_option,
            "visualRationale": result.visual_rationale,
            "queryPlan": result.query_plan.to_dict() if result.query_plan else None,
        })

    @staticmethod
    def _singleStepOverview(description: str, sub_question: str) -> StreamEvent:
        """单步查询的执行计划概览事件（用于统一展示 MultiStepPlanCard）。

        序列前缀：multi_step_plan → step_plan（由调用方紧接 yield）→ ... → step_result。
        与 _streamMultiStep 的多步事件序列同构，前端 handler 无需区分单/多步。
        """
        return StreamEvent(EVENT_MULTI_STEP_PLAN, {
            "steps": [{
                "stepIndex": 0,
                "description": description,
                "subQuestion": sub_question,
                "aggregationOnly": False,
            }],
        })

    @staticmethod
    def _singleStepStart(description: str, sub_question: str) -> StreamEvent:
        """单步查询的步骤进入事件。"""
        return StreamEvent(EVENT_STEP_PLAN, {
            "stepIndex": 0,
            "description": description,
            "subQuestion": sub_question,
        })

    async def persistInterruptedStream(
        self, dto: ChatRequest, session: AsyncSession
    ) -> None:
        """断连兜底落库（H4）：把已下发给客户端的部分产出写进历史并标记中断。

        由 API 层在 SSE 响应收尾时经 `StreamingResponse(background=...)` 调用：该钩子
        在 Starlette 的收敛任务组**之外** await（断连时确定会跑到），因此不需要分离任务。

        `session`（请求作用域）**只用来读本轮快照**，写入走**独立会话** —— 实测结论，
        不是防御性写法：取消是在最近一个 await 上打进来的（实测落在本轮 `_recordUsage`
        的 `commit() → flush()` 中途），请求会话随即被标成 needs-rollback（直接写抛
        `PendingRollbackError`）；即便先 `rollback()` 复原，底层 asyncpg 连接也已被关掉
        而 SQLAlchemy 并未察觉（探针实测 `pg_closed=True` 同时 `invalidated=False`），
        下一条语句即整条失败于 `InterfaceError: connection is closed`。注意这不是
        `pool_pre_ping` 能兜住的场景：连接是在**被持有期间**死掉的，pre_ping 只在签出时
        检查。请求会话在取消之后**不是可靠的写入通道**，故不复用：多一条连接，换兜底必达。

        首行短路：本轮已落库（`pending=False`）⇒ 什么都不写（单发标志去重）。正常跑完
        的请求、以及「先落库后 yield」的各条路径（闲聊/澄清/领域命令/卡片/多步）都在
        落库时已解除标记，故本方法对它们是空操作。

        计量的诚实性：答复 token 只随 `isDone` 终块到达（非终块发 0/0）⇒ 断连时刻部分
        答案的 token 数**根本不存在**，不做回填、不写假账。`token_cost_usd` 只含已测得
        的部分（计划/SQL/图表/已完成的调用），是真实成本的下界；断连那一轮的答复用量行
        随被取消的 flush 一起没了（它本就未提交），SSOT 已注明这是下界而非测得值。
        """
        state = streamPersistStateOf(session)
        if state is None or not state.pending:
            return
        state.pending = False  # 兜底自身也只写一次（background 重复调用/并发都安全）
        answer = "".join(state.answerPieces) or MSG_STREAM_INTERRUPTED_EMPTY

        from app.infrastructure.database import getSessionFactory

        async with getSessionFactory()() as fallbackSession:
            await self._storeSessionMessages(
                fallbackSession, dto.sessionId, dto.question, answer, state.sql,
                routing_layer=ROUTING_LAYER_L2,
                latency_ms=int((time.monotonic() - state.startedAt) * 1000),
                token_cost_usd=state.totalCostUsd,
                interrupted=True,
            )
            # 查询状态照常保存：否则下一轮的追问（REFINE/FOLLOW_UP）失去锚点
            await self._saveQueryState(
                fallbackSession, dto.sessionId,
                question=dto.question, plan=state.plan, sql=state.sql,
                resultColumns=state.resultColumns,
            )
        logger.info(
            "流式断连兜底落库: session=%s 已下发片段=%d 内容长度=%d",
            dto.sessionId, len(state.answerPieces), len(answer),
        )

