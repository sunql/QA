"""多步执行 / 追问级联（ChatService 的 MultiStepMixin）。

拆步判定 → 显式分步解析 → 追问改写/多步重跑（B）→ 兜底重试（C）→ 逐步执行
（生成 → 执行 + 回灌重试）→ 聚合/降级收尾。流式与非流式共用 `_executeDataStep`
与 `_finalizeMultiStepDegrade`，保证两条路径同源不漂移。
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import IntentType
from app.domain.models import LlmConfig, SessionQueryState
from app.domain.multi_step_plan import (
    GlobalFilters,
    MultiStepPlan,
    StepExecutionContext,
    StepPlan,
    StepResult,
)
from app.domain.query_plan import QueryPlan
from app.domain.schemas import ChatRequest, ChatResponse
from app.infrastructure.llm.base_client import LlmMessage
from app.services.chat_helpers import (
    _MSG_STEP_UNANSWERABLE,
    _PipelineContext,
    _STEP_EXEC_FAILED_PREFIX,
    _STEP_GEN_FAILED_PREFIX,
    _StepRun,
    _failedStepResult,
    _hasDataStepResult,
    _step_result_to_read,
    _stepFailedError,
)
from app.services.messages_zh import (
    MSG_MULTI_STEP_DEGRADE_FAILED,
    MSG_MULTI_STEP_DEGRADE_PARTIAL,
)
from app.services.step_query_planner import StepPlanResult, StepQueryPlanner

logger = logging.getLogger(__name__)

# feat-follow-up-cascade C 兜底：触发追问重试的短句上限。省略式追问（"4月份呢？"）
# 是短句特征；长句不可回答更可能是真正的新问题（N6 权衡），重试只会空耗 token。
_FOLLOW_UP_RETRY_MAX_LEN = 20


class MultiStepMixin:
    """多步执行 / 追问级联（由 ChatService 组合）。"""

    async def _detectMultiStep(
        self, session: AsyncSession, dto: ChatRequest, pc: _PipelineContext,
    ) -> StepPlanResult | None:
        """判定当前问题是否需要多步拆解；返回 StepPlanResult 或 None（走原流水线）。

        仅对 NEW_QUERY / QUERY 意图触发；REFINE/FOLLOW_UP 走原单轮流水线
        （避免对上一轮 SQL 的微调被强制拆步）。

        拆步 LLM 调用无论结果如何都计量（purpose="step_plan"）；调用抛异常时
        返回 None 且不计量（调用未成功，无可计量 token）。
        """
        try:
            result = await self._stepPlanner.plan(
                dto.question, pc.classes, pc.client, pc.selected.model_name,
            )
        except Exception:
            logger.warning("拆步判定失败，回退单步: %s", dto.question, exc_info=True)
            return None
        # 拆步 LLM 调用已发生，如实计量（即使拆出单步/解析失败）
        await self._recordUsage(
            session, dto.sessionId, pc.selected,
            result.prompt_tokens, result.completion_tokens, purpose="step_plan",
        )
        if result.plan is None or result.plan.is_single_step:
            return None
        return result

    async def _resolveExplicitMultiStep(
        self, session: AsyncSession, dto: ChatRequest, pc: _PipelineContext,
    ) -> tuple[MultiStepPlan | None, int, Decimal]:
        """解析显式分步信号：先规则快路径（第X步标号），失败回退 LLM 拆步。

        返回 (multi_plan, step_tokens, step_cost)；plan 为 None 表示未拆出多步，
        调用方按原流水线走单步。规则命中时 token=0（零 LLM 调用）。

        规则路径仍记一条 token=0 的 step_plan 审计行（与 LLM 拆步同 purpose），
        保证"按 purpose 聚合"的下游分析能一致统计所有多步拆解事件，包括零成本
        的规则命中（成本/审计一致性 2026-08-16 修复）。
        """
        rule_result = await self._stepPlanner.plan_explicit(dto.question)
        if rule_result.plan is not None:
            await self._recordUsage(
                session, dto.sessionId, pc.selected, 0, 0, purpose="step_plan",
            )
            return rule_result.plan, 0, Decimal("0")

        detected = await self._detectMultiStep(session, dto, pc)
        if detected is None or detected.plan is None:
            return None, 0, Decimal("0")
        step_tokens = detected.prompt_tokens + detected.completion_tokens
        step_cost = self._costFor(
            pc.selected, detected.prompt_tokens, detected.completion_tokens,
        )
        return detected.plan, step_tokens, step_cost

    # =========================================================================
    # feat-follow-up-cascade：追问级联（B 多步重跑 / C 兜底重试）
    # =========================================================================

    @staticmethod
    def _isFollowUpRetryCandidate(
        question: str, intent: IntentType, state: SessionQueryState | None,
    ) -> bool:
        """C 兜底谓词：NEW_QUERY/QUERY 计划不可回答 + 有历史 + 短句 → 升级追问重试。

        约束逐项是独立闸门：非新查询意图（REFINE/FOLLOW_UP 已有自己的状态注入路径，
        再重试只是重复）/ 上一轮无成功 SQL（追问没有可锚定的查询）/ 长句（守 N6：
        长句更可能是真正的新问题）均不重试。
        """
        if intent not in (IntentType.NEW_QUERY, IntentType.QUERY):
            return False
        if state is None or not (state.last_question or "").strip() or not state.last_sql:
            return False
        normalized = question.strip()
        if len(normalized) > _FOLLOW_UP_RETRY_MAX_LEN:
            return False
        return not StepQueryPlanner.is_explicit_multi_step(normalized)

    async def _rewriteFollowUpQuestion(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        pc: _PipelineContext,
        state: SessionQueryState,
    ) -> tuple[str, int, int] | None:
        """B 改写：把省略式追问合并进上一轮完整问题。

        返回 (改写后问题, promptTokens, completionTokens)。LLM 调用失败 / 回复
        不可解析 / 改写无效（空串，或只是回显追问本身）时返回 None，由调用方
        退回单轮 FOLLOW_UP 状态注入。已发生的 token 无论成败都计量（purpose=
        "follow_up_rewrite"），与用户输入经 StepQueryPlanner._sanitize 转义。

        与上一轮逐字相同的改写是**有效**结果（追问合并后并未改变原问题，例如用
        户在上一轮失败后原样重发同一追问）——此时应把上一轮多步问题重跑一遍，
        而非判为无效退回。旧守卫把这一情形一并丢弃，导致重发追问必然退化成
        4 字短句单轮查询 → LLM 判无有效查询计划 → "无法回答"。
        """
        prior = (state.last_question or "").strip()
        prompt = (
            "你是问题改写器。用户上一轮完整问题：\n"
            f"{StepQueryPlanner._sanitize(prior)}\n\n"
            "用户现在的追问：\n"
            f"{StepQueryPlanner._sanitize(dto.question.strip())}\n\n"
            "把追问里的改动（如时间/条件替换）合并进上一轮完整问题，保持原有步骤"
            "结构，只应用追问的改动。输出 JSON：{\"question\": \"改写后的完整问题\"}；"
            "若追问无法合并进上一轮问题，输出 {\"question\": \"\"}。"
        )
        try:
            resp = await pc.client.complete(
                messages=[
                    LlmMessage(role="system", content="你是问题改写器，只输出 JSON。"),
                    LlmMessage(role="user", content=prompt),
                ],
                model=pc.selected.model_name,
            )
        except Exception:
            logger.warning("追问改写 LLM 调用失败，退回单轮注入: %s", dto.question, exc_info=True)
            return None
        await self._recordUsage(
            session, dto.sessionId, pc.selected,
            resp.promptTokens, resp.completionTokens, purpose="follow_up_rewrite",
        )
        data = StepQueryPlanner._extract_json(resp.content or "")
        if not data:
            return None
        rewritten = data.get("question")
        if not isinstance(rewritten, str):
            return None
        rewritten = rewritten.strip()
        # 只丢弃「空串」与「回显追问本身」（合并未发生）两种无效改写；
        # 与 prior 相同视为有效——含义是"照上一轮的问题重跑一遍"。
        if not rewritten or rewritten == dto.question.strip():
            return None
        return rewritten, resp.promptTokens, resp.completionTokens

    async def _prepareFollowUpMultiStep(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        pc: _PipelineContext,
        state: SessionQueryState,
    ) -> tuple[ChatRequest, MultiStepPlan, int, Decimal, GlobalFilters | None] | None:
        """B 共享前置：上一轮是多步问题时，改写追问、拆出多步计划、抽取全局约束。

        返回 (改写后 dto, 多步计划, 累计 tokens（改写+拆步）, 累计 cost,
        改写后问题的全局约束)。非多步上一轮 / 改写失败 / 拆不出多步 → None，
        调用方退回单轮 FOLLOW_UP。

        非流式（_handleGenericQuery）与流式（_streamQuery）两入口共用本方法，
        保证级联口径一致。

        **全局约束在此抽取（而非各调用方自取）**：多步执行需要的是「改写后问题」
        的口径约束，与多步计划同源，故与计划一起产出。此前由每个入口各写一次
        `_resolveGlobalFilters(dto2)` —— 结果 C 兜底入口（非流式/流式）漏写，
        同一段多步代码因入口不同丢掉全局 WHERE（P0 对称缺口）。收敛到 SSOT 后，
        新增入口不可能再漏。
        """
        prior = (state.last_question or "").strip()
        if not self._stepPlanner.is_explicit_multi_step(prior):
            return None
        rewritten = await self._rewriteFollowUpQuestion(session, dto, pc, state)
        if rewritten is None:
            return None
        question, rwPt, rwCt = rewritten
        dto2 = dto.model_copy(update={"question": question})
        multiPlan, stepTokens, stepCost = await self._resolveExplicitMultiStep(
            session, dto2, pc,
        )
        if multiPlan is None:
            return None
        # 重写后问题可能改变口径约束：按 dto2 重抽（而非沿用 dto 的）
        globalFilters = await self._resolveGlobalFilters(session, dto2, pc)
        totalTokens = stepTokens + rwPt + rwCt
        totalCost = stepCost + self._costFor(pc.selected, rwPt, rwCt)
        return dto2, multiPlan, totalTokens, totalCost, globalFilters

    async def _resolveGlobalFilters(
        self, session: AsyncSession, dto: ChatRequest, pc: _PipelineContext,
    ) -> GlobalFilters | None:
        """B 层（feat-multistep-global-filter）：预抽取多步问题的全局范围类约束。

        调用一次 LLM（purpose="multistep_global_filter"），失败降级返回 None——
        仅靠 A 的措辞兜底，不阻断多步执行。被抽取的约束将注入每一步 prompt
        的 [global_constraints] 块，强指令 LLM 沿用。

        **计量**（H1）：抽取本身是一次 LLM 调用，token 由 ``extract_global_filters``
        如实交回（此前该方法丢弃 tokens、这里写 0/0 假审计行，且注释谎称「token 已计到
        plan/split 路径」）。只要真的花掉了 token 就落台账——即便解析失败导致 gf 为
        None，钱也已经花了。
        """
        try:
            gf, promptTokens, completionTokens = await self._stepPlanner.extract_global_filters(
                dto.question, pc.classes, pc.client, pc.selected.model_name,
            )
        except Exception:
            logger.warning(
                "全局过滤抽取异常，降级仅靠措辞: %s", dto.question, exc_info=True,
            )
            return None
        if promptTokens or completionTokens:
            await self._recordUsage(
                session, dto.sessionId, pc.selected, promptTokens, completionTokens,
                purpose="multistep_global_filter",
            )
        return gf

    async def _executeDataStep(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        pc: _PipelineContext,
        ctx: StepExecutionContext,
        step_plan: StepPlan,
        state: SessionQueryState | None,
    ) -> _StepRun:
        """执行单个数据步骤（两阶段生成 → 执行 + 回灌重试），失败隔离为步骤错误行。

        C3 修复：流式（`_streamMultiStep`）与非流式（`_executeMultiStep`）共用本
        helper，两条路径自此同源（此前的教训是「只改一条路径」必然漂移）。

        此前步骤内的硬失败（生成抛错 / 执行 + 回灌重试均失败）直接穿透到 API 层：
        已完成步骤的数据与已花的 token 全部作废，对外 500 / internal 错误事件。
        现在收敛为 `StepResult(error=..., sql=None)` —— sql=None 是失败标记，
        同时保证「无数据」不会被下游 prompt 渲染成「结果为 0 行」。
        """
        injection_text = ctx.inject_to_prompt(step_plan.index)
        try:
            outcome = await self._planAndGenerateSql(
                session, dto, pc, IntentType.NEW_QUERY, state,
                sub_question=step_plan.sub_question, injection_text=injection_text,
                global_filters=ctx.global_filters,
            )
        except Exception as exc:
            logger.warning(
                "多步步骤查询生成失败，隔离该步骤: step=%s", step_plan.index, exc_info=True,
            )
            return _StepRun(result=_failedStepResult(
                step_plan, _stepFailedError(exc, _STEP_GEN_FAILED_PREFIX),
            ))

        # 生成阶段的 token 无论后续是否执行成功都已花掉，照旧计入总量（核心约束 #3）
        tokens = (
            outcome.promptTokens + outcome.completionTokens
            + outcome.wasted[0] + outcome.wasted[1]
        )
        cost = self._costForSql(outcome, pc.selected)
        model_name = (outcome.sqlConfig or pc.selected).model_name

        if outcome.sql is None or outcome.plan is None or outcome.plan.isUnanswerable:
            # 软失败（LLM 判定无法回答）：非硬异常，一直就是步骤级隔离
            return _StepRun(
                result=_failedStepResult(step_plan, _MSG_STEP_UNANSWERABLE),
                tokens=tokens, cost=cost, modelName=model_name,
            )

        try:
            data, final_sql, retry_tokens = await self._runQueryWithRetry(
                session, dto, pc, outcome,
                # C4：重试必须沿用本步骤的子问题与前序注入（否则退回原始复合问题，
                # 丢子问题范围与「前序步骤结果」约束，与本步首次生成口径不一致）
                question=step_plan.sub_question, prior_state=injection_text,
                # 主问题作 scopeQuestion（多步显式传入，单步调用方不传）：供计划阶段的
                # 「主问题 ∪ 子问题」并集判定沿用
                scope_question=dto.question,
            )
        except Exception as exc:
            logger.warning(
                "多步步骤执行失败（含回灌重试），隔离该步骤: step=%s",
                step_plan.index, exc_info=True,
            )
            # 回灌重试的**生成** token 也随异常交回：重试生成成功、重试执行又失败时，
            # 那次生成同样花了钱，必须落账并计入总量（核心约束 #3——失败路径也是计量路径）
            retryUsage = await self._accountRetryGenUsage(session, dto, exc, pc, outcome)
            if retryUsage is not None:
                tokens += retryUsage.tokens
                cost += retryUsage.cost
                model_name = retryUsage.modelName
            return _StepRun(
                result=_failedStepResult(step_plan, _stepFailedError(exc, _STEP_EXEC_FAILED_PREFIX)),
                tokens=tokens, cost=cost, modelName=model_name,
            )

        if retry_tokens[0] or retry_tokens[1]:
            retry_cfg = outcome.sqlConfig or pc.selected
            tokens += retry_tokens[0] + retry_tokens[1]
            cost += self._costFor(retry_cfg, retry_tokens[0], retry_tokens[1])
            await self._recordUsage(
                session, dto.sessionId, retry_cfg,
                retry_tokens[0], retry_tokens[1], purpose="nl2sql",
            )
            model_name = retry_cfg.model_name

        # 后台存储查询向量（用子问题，便于 few-shot 精确匹配）
        self._spawnEmbedding(dto, final_sql, question=step_plan.sub_question)
        return _StepRun(
            result=StepResult(
                step_index=step_plan.index,
                description=step_plan.description,
                sub_question=step_plan.sub_question,
                sql=final_sql,
                data=data,
                summary=self._summarizeStepData(data),
            ),
            tokens=tokens, cost=cost, modelName=model_name, plan=outcome.plan,
        )

    async def _executeMultiStep(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        pc: _PipelineContext,
        multiStepPlan: MultiStepPlan,
        state: SessionQueryState | None,
        *,
        initial_tokens: int = 0,
        initial_cost: Decimal = Decimal("0"),
        _t0: float,
        global_filters: GlobalFilters | None = None,
    ) -> ChatResponse:
        """顺序执行每个子步骤，最后调用 StepAggregator 汇总，返回完整多步响应。

        每步复用 _planAndGenerateSql（两阶段 + 重试降级）；
        每步失败记录 error 字段，不阻断后续步骤（线性多步，失败隔离）；
        最后一步聚合调用 StepAggregator 生成最终回答。

        initial_tokens/initial_cost：进入多步前已消耗的 token/成本（拆步判定、或
        单步失败回退时已消耗的单步生成），计入响应 tokensUsed/cost，保证与审计行一致。

        _t0：调用方传入的计时起点（来自 _handleGenericQuery 入口计时）。
        """
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
        # 最后一个成功数据步骤的 plan/sql/data，用于保存查询状态支持下一轮追问
        last_plan: QueryPlan | None = None
        last_sql: str | None = None
        last_data: list[dict] = []

        for step_plan in multiStepPlan.steps:
            if step_plan.aggregation_only:
                if not _hasDataStepResult(completed):
                    # 所有数据步骤都失败：汇总 LLM 拿到的只有错误行，只会编造结论
                    # ⇒ 跳过汇总，落到循环后的降级收尾（如实告知失败）
                    logger.warning("多步数据步骤全部失败，跳过汇总步骤")
                    continue
                # 汇总步骤：跳过 SQL 执行，调用 StepAggregator
                agg_resp = await self._callWithFallback(
                    session, dto.sessionId, pc.configs, pc.selected, "answer",
                    lambda cfg: self._stepAggregator.aggregate(
                        dto.question, multiStepPlan, completed,
                        self._llmFactory(cfg), cfg.model_name,
                        history=pc.contextPrompt,
                    ),
                    forced=pc.forcedModel,
                )
                agg_content = agg_resp[0].content
                agg_config = agg_resp[1]
                agg_pt = agg_resp[0].promptTokens
                agg_ct = agg_resp[0].completionTokens
                wasted_pt, wasted_ct = agg_resp[2]
                total_tokens += agg_pt + agg_ct + wasted_pt + wasted_ct
                total_cost += self._costFor(agg_config, agg_pt, agg_ct)
                total_cost += self._costFor(pc.selected, wasted_pt, wasted_ct)
                last_model_name = agg_config.model_name
                await self._recordUsage(
                    session, dto.sessionId, agg_config,
                    agg_pt, agg_ct, purpose="answer",
                )
                await self._storeSessionMessages(
                    session, dto.sessionId, dto.question, agg_content, None,
                    routing_layer="L2",
                    latency_ms=int((time.monotonic() - _t0) * 1000),
                    token_cost_usd=float(total_cost),
                )
                # 保存查询状态：用最后一个数据步骤的 plan/sql，支持下一轮 REFINE/FOLLOW_UP
                await self._saveQueryState(
                    session, dto.sessionId,
                    question=dto.question, plan=last_plan, sql=last_sql,
                    resultColumns=self._columns(last_data),
                )
                affinity = await self._buildAffinityStatus(
                    session, dto.sessionId, agg_config.id, agg_config.model_name,
                )
                return ChatResponse(
                    answer=agg_content,
                    intent="multi_step",
                    steps=[_step_result_to_read(s) for s in completed],
                    tokensUsed=total_tokens,
                    cost=float(total_cost),
                    latency_ms=int((time.monotonic() - _t0) * 1000),
                    modelName=last_model_name,
                    affinityStatus=affinity,
                    classRecall=pc.recall,
                )

            # 数据查询步骤：共用 helper（生成 → 执行 + 回灌重试），失败隔离为 error 行
            run = await self._executeDataStep(session, dto, pc, ctx, step_plan, state)
            total_tokens += run.tokens
            total_cost += run.cost
            if run.modelName:
                last_model_name = run.modelName
            completed.append(run.result)
            ctx = ctx.with_step(run.result)
            if run.result.sql is not None:
                # 只有成功步骤才更新追问锚点：失败步骤没有 SQL/数据可作下一轮基准
                last_plan = run.plan
                last_sql = run.result.sql
                last_data = run.result.data

        # 所有步骤都不是 aggregation_only（异常），降级为普通回答
        answer = await self._finalizeMultiStepDegrade(
            session, dto, completed,
            last_plan=last_plan, last_sql=last_sql, last_data=last_data,
            total_cost=total_cost, _t0=_t0,
        )
        return ChatResponse(
            answer=answer,
            intent="multi_step",
            steps=[_step_result_to_read(s) for s in completed],
            tokensUsed=total_tokens,
            cost=float(total_cost),
            latency_ms=int((time.monotonic() - _t0) * 1000),
            modelName=last_model_name,
        )

    async def _finalizeMultiStepDegrade(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        completed: list[StepResult],
        *,
        last_plan: QueryPlan | None,
        last_sql: str | None,
        last_data: list[dict],
        total_cost: Decimal,
        _t0: float,
    ) -> str:
        """无汇总步骤时的降级收尾：落库 + 保存查询状态，返回给用户的文案。

        流式（`_streamMultiStep`）与非流式（`_executeMultiStep`）共用，避免再次
        出现「只改一条路径」的偏差。

        此前两条路径都只返回文案、**不写状态**：assistant 消息缺失（历史里留下
        悬空的 user 轮），`last_question`/`last_sql` 停在上一轮 → 下一轮追问会
        锚到更早的问题（静默答错）或退化成无锚点的单轮查询。文案也如实区分
        「数据步已成功、只差汇总」与「整体失败」，前者不该说成「执行异常」。
        """
        succeeded = [r for r in completed if r.sql is not None]
        if succeeded:
            answer = MSG_MULTI_STEP_DEGRADE_PARTIAL.format(
                done=len(succeeded), total=len(completed),
            )
        else:
            answer = MSG_MULTI_STEP_DEGRADE_FAILED
        logger.warning(
            "多步执行异常：无可用的 aggregation 步骤，降级收尾（完成 %d/%d 步）",
            len(succeeded), len(completed),
        )
        await self._storeSessionMessages(
            session, dto.sessionId, dto.question, answer, last_sql,
            routing_layer="L2",
            latency_ms=int((time.monotonic() - _t0) * 1000),
            token_cost_usd=float(total_cost),
        )
        await self._saveQueryState(
            session, dto.sessionId,
            question=dto.question, plan=last_plan, sql=last_sql,
            resultColumns=self._columns(last_data),
        )
        return answer

    def _summarizeStepData(self, data: list[dict]) -> str:
        """生成数据的一句话摘要（数值列的 max/min/sum）。空数据返回'（无数据）'。

        数值列识别同时覆盖 int/float/Decimal（业务库数值列常以 Decimal 返回）。
        """
        if not data:
            return "（无数据）"
        try:
            numeric_keys = [
                k for k, v in data[0].items()
                if isinstance(v, (int, float, Decimal))
            ]
            if not numeric_keys:
                return f"（{len(data)} 行结果）"
            parts = []
            for key in numeric_keys[:3]:
                vals = [
                    row[key] for row in data
                    if isinstance(row.get(key), (int, float, Decimal))
                ]
                if vals:
                    parts.append(f"{key} 范围: {min(vals):.2f}~{max(vals):.2f}")
            return "; ".join(parts) if parts else f"（{len(data)} 行结果）"
        except Exception:
            return f"（{len(data)} 行结果）"
