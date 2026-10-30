"""流式查询意图编排（ChatService 的 StreamQueryMixin）。

`_streamQuery` 是查询意图（NEW_QUERY / QUERY / FOLLOW_UP）的流式主路径：
plan → sql → chart → token×N → done（含持久化与状态保存），失败时回退多步。
承担 FEATURE 命中短路、单步/多步路由分流、回答流（含 Think_Hide 过滤）、
L2 歧义示警、断连兜底快照挂载等核心能力。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import IntentType
from app.domain.models import SessionQueryState
from app.domain.multi_step_plan import StepResult
from app.domain.query_plan import QueryPlan
from app.domain.schemas import AgentSuggestion, ChatRequest, SemanticState
from app.services.chart_thresholds import loadFullDataThreshold
from app.services.chat_constants import (
    ROUTING_LAYER_L2,
    USAGE_PURPOSE_ANSWER,
    USAGE_PURPOSE_NL2SQL,
)
from app.services.chat_helpers import (
    _looks_like_compound_question,
    _PipelineContext,
    _clipText,
    streamPersistStateOf,
)
from app.services.kpi_contract_reconcile import buildKpiDenominatorWarning
from app.services.nl2sql_service import _readFloatConfig
from app.services.nl2sql_semantic_guard import shareAmbiguityWarning
from app.services.stream_events import (
    EVENT_CHART,
    EVENT_CLASS_RECALL,
    EVENT_DATA_QUALITY,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_MULTI_STEP_PLAN,
    EVENT_PLAN,
    EVENT_SQL,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
    StreamEvent,
)
from app.services.think_block import ThinkStreamFilter, isThinkHideEnabled

logger = logging.getLogger(__name__)


class StreamQueryMixin:
    """流式查询意图编排（由 ChatService 组合）。"""

    async def _streamQuery(
        self, dto: ChatRequest, session: AsyncSession, intent: IntentType, state: SessionQueryState | None,
        intentChartType=None,
        suggestion: AgentSuggestion | None = None,
        semanticState: SemanticState | None = None,
        *,
        _t0: float | None = None,
        user: CurrentUser | None = None,  # P0-3
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
                        user=user,
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
                        user=user,
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
                    user=user,
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
                    user=user,
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
            # R1 fix: 提前初始化 EVENT_DONE 引用变量（定义在 line 541 return 之后）
            totalTokens = outcome.promptTokens + outcome.completionTokens + outcome.wasted[0] + outcome.wasted[1]
            totalCost = self._costForSql(outcome, pc.selected, cacheHitMultiplier=cacheHitMultiplier)
            affinityConfig = type("FakeAffinity", (), {"model_name": None})()
            affinityPayload = None
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
                # F1：dbUserId 是 int 主键，对齐 session_query_state.user_id 的 BigInteger
                userId=user.dbUserId if user is not None else None,
                datasourceId=dto.datasourceId,
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
        # Task 12.5 review Critical：必须透传 user=user，Feature 路径 _saveQueryState
        # 写 userId=None 会写 (NULL, ds, session) 行，P0-3 三元 scope 读不到，状态被
        # 全局复用。与非流式入口（chat_service.py:609）口径一致。
        featureResp = await self._tryFeatureResponse(session, dto, pc, outcome, user=user)
        if featureResp is not None:
            await self._storeSessionMessages(session, dto.sessionId, dto.question, featureResp.answer, None)
            await self._saveQueryState(
                session, dto.sessionId,
                question=dto.question, plan=outcome.plan, sql=None, resultColumns=[],
                # F1：dbUserId 是 int 主键，对齐 session_query_state.user_id 的 BigInteger
                userId=user.dbUserId if user is not None else None,
                datasourceId=dto.datasourceId,
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

        # Feature 未命中：进入正常 retry 路径，初始化 SQL 总 token/cost。
        # 注：此计算原本在 line 603（featureResp 之前），reviewer 误判为死计算；
        # 实际是 featureResp is None 路径下的初值（被 line 704+ 的 `+=` 累加）。
        # 移到此处后 `featureResp is not None` 路径不再浪费一次 _costForSql 调用。
        totalTokens = outcome.promptTokens + outcome.completionTokens + outcome.wasted[0] + outcome.wasted[1]
        totalCost = self._costForSql(outcome, pc.selected, cacheHitMultiplier=cacheHitMultiplier)

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
                        user=user,
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
        # B4 分母契约弱校验（kpi-governance B4）：治理声明分母列未出现在生成
        # SQL 的分母表达式中 → 示警前置（L2 出口，不硬拒）。快照 answerPieces
        # 同引用，先追加后 yield 保证断连兜底可见（同 shareWarning 约定）。
        denominatorWarning = await buildKpiDenominatorWarning(
            session, dto.question, finalSql
        )
        if denominatorWarning:
            answerPieces.append(denominatorWarning)
            yield StreamEvent(EVENT_TOKEN, {"content": denominatorWarning})
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
            # F1：本行此前漏传 scope，导致默认（流式）路径写出的行 user_id/datasource_id
            # 恒 NULL —— 下一轮按 scope 过滤时 NULL ≠ uid，追问读不到自己的锚点。
            # `user` 是 `_streamQuery` 的 keyword-only 形参（由 `_processMessageStreamInner`
            # 从 API 层的 `Depends(getCurrentUser)` 一路透传，见 L567/L608）。
            userId=user.dbUserId if user is not None else None,
            datasourceId=dto.datasourceId,
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


__all__ = ["StreamQueryMixin"]
