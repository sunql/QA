"""流式多步编排（ChatService 的 StreamMultiStepMixin）。

`_streamMultiStep` 是多步查询意图的流式事件序列：step_plan/step_result × N
→ token(汇总) → done。与非流式 `_executeMultiStep` 同构，但逐步 yield 中间
步骤事件，前端可实时渲染每一步的 SQL 与数据。每步复用 `_planAndGenerateSql`
（两阶段 + 重试降级），步骤失败记录 error 不阻断后续步骤。

模块级助手 `_overviewStepPayload` 从 `chat_stream_entry` 导入，用于续跑时
`index < startIndex` 的步回放已持久化的终态（而不是退回 pending）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.multi_step_plan import (
    GlobalFilters,
    MultiStepPlan,
    StepExecutionContext,
    StepResult,
)
from app.domain.query_plan import QueryPlan
from app.domain.schemas import AgentSuggestion, ChatRequest
from app.services import multi_step_persistence as persistence
from app.services.chart_thresholds import loadFullDataThreshold
from app.services.chat_constants import (
    ROUTING_LAYER_L2,
    USAGE_PURPOSE_ANSWER,
)
from app.services.chat_context import InheritedState
from app.services.chat_helpers import (
    _MSG_STEP_AGGREGATION_SKIPPED,
    _PipelineContext,
    _failedStepResult,
    _hasDataStepResult,
    _step_result_to_read,
)
from app.services.chat_stream_entry import _overviewStepPayload
from app.services.multi_step_persist_hooks import _maxInputTokens, runStatusFor
from app.services.multi_step_retry import ERROR_KIND_PERMANENT
from app.services.nl2sql_service import _readFloatConfig
from app.services.think_block import applyThinkPolicy
from app.services.stream_events import (
    EVENT_DONE,
    EVENT_MULTI_STEP_PLAN,
    EVENT_STEP_COMPRESSED,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
    StreamEvent,
)
from app.services.visual_rationale import summaryTextOnlyRationale

logger = logging.getLogger(__name__)


class StreamMultiStepMixin:
    """流式多步编排（由 ChatService 组合）。"""

    async def _streamMultiStep(
        self,
        dto: ChatRequest,
        session: AsyncSession,
        pc: _PipelineContext,
        multiStepPlan: MultiStepPlan,
        state,
        *,
        initial_tokens: int = 0,
        initial_cost: Decimal = Decimal("0"),
        suggestion: AgentSuggestion | None = None,
        _t0: float | None = None,
        global_filters: GlobalFilters | None = None,
        semanticState: InheritedState | None = None,
        priorSnapshot: dict | None = None,
        user: CurrentUser | None = None,  # P0-3
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
                userId=user.dbUserId if user is not None else None,
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
                    # F1：同 L802，`_streamMultiStep` 的 `user` 也是 keyword-only 形参，
                    # 由 5 个调用点统一透传（见 L467/L486/L503/L523/L676）
                    userId=user.dbUserId if user is not None else None,
                    datasourceId=dto.datasourceId,
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
            userId=user.dbUserId if user is not None else None,
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


__all__ = ["StreamMultiStepMixin"]
