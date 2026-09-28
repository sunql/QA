"""查询执行 / 用量 / 成本 / 图表（ChatService 的 UsageMixin）。

SQL 执行（含回灌重试）、图表生成、自然语言回答三条流水线段，加上贯穿全程的
Token 计量与成本落账：`_recordUsage`（有 ModelConfig）/ `_recordDirectUsage`
（Agent/Tool 路径无配置透传）两条写入口，`_costFor`/`_costForSql`/`_summarizeUsage`
计算成本，`_consumedTokens` 从异常回收失败路径用量（M4）。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ChartType
from app.domain.models import LlmConfig
from app.domain.schemas import ChatRequest
from app.infrastructure.llm.base_client import LlmMessage
from app.services.chat_helpers import (
    _PipelineContext,
    _RETRY_SQL_LOG_LIMIT,
    _RetryGenUsage,
    _SqlOutcome,
    _attachRetryFailure,
    _clipText,
    _summarizeExecutionError,
)
from app.services.chat_stream_output import _ANSWER_SYSTEM_PROMPT
from app.services.llm_retry_policy import (
    attachRetryGenTokens as _attachRetryGenTokens,
    consumedTokens,
    retryGenTokens as _retryGenTokens,
)

logger = logging.getLogger(__name__)


class UsageMixin:
    """查询执行 / 用量 / 成本 / 图表（由 ChatService 组合）。"""

    async def _runQuery(
        self,
        pc: _PipelineContext,
        dto: ChatRequest,
        sql: str,
        *,
        user_id: str | None = None,
    ) -> list[dict]:
        adapter = self._adapterProvider(dto.datasourceId, pc.ds)
        return await adapter.execute_read_only(sql)

    async def _runQueryWithRetry(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        pc: _PipelineContext,
        outcome: _SqlOutcome,
        *,
        question: str | None = None,
        prior_state: str | None = None,
        scope_question: str | None = None,
    ) -> tuple[list[dict], str, tuple[int, int]]:
        """执行 SQL；执行报错时回灌错误重试一轮（1-3）。

        旧实现把生成阶段通过校验的 SQL 直接交给适配器执行，执行报错（表名/列名拼错、
        方言差异等）即整体失败，对外暴露"服务内部错误"。此处捕获执行异常，把错误信息
        回灌给 generateSql 重新生成一次 SQL；重试仍失败则抛原始执行错误（保持原有
        可观测行为不变）。

        返回 (数据, 最终生效 SQL, 重试额外消耗的 prompt/completion token 二元组)；
        未触发重试时第三元为 (0, 0)。不改动入参。

        question / prior_state / scope_question（C4）：多步场景下调用方传入子问题、前序
        步骤注入文本与主问题，使重试生成的上下文与本步骤**首次**生成一致。此前恒用
        `dto.question`（原始复合问题）且 priorState=None，重试会丢掉子问题范围与
        「前序步骤结果」约束（如第二步引用的「这三个供应商」），生成的 SQL 重新对齐成
        整个复合问题。单步场景三个参数都不传（保持旧行为）：question=None → 用
        dto.question、priorState=None、scopeQuestion=None。

        重试**生成**成功但重试执行仍失败、以及重试**生成自己**失败，这两种情形下重试
        那次调用的 token 都由异常携带交回调用方（见 `_attachRetryGenTokens`），避免
        「花了钱但失败路径不记账」。
        """
        retry_question = question if question is not None else dto.question
        try:
            data = await self._runQuery(pc, dto, outcome.sql)
            return data, outcome.sql, (0, 0)
        except Exception as firstErr:
            cfg = outcome.sqlConfig or pc.selected
            logger.info("SQL 执行失败，回灌错误重试一轮: %s", firstErr)
            try:
                retryResult = await self._nl2sql.generateSql(
                    retry_question, pc.classes, self._llmFactory(cfg), cfg,
                    plan=outcome.plan,
                    datasourceType=pc.ds.type, oracle_version=pc.ds.oracle_version,
                    schemaPrefix=pc.ds.username,
                    context=pc.contextPrompt, priorState=prior_state,
                    executionError=_summarizeExecutionError(firstErr),
                    fewShot=pc.fewShot, valueSamples=pc.valueSamples,
                    driftWarning=pc.driftWarning, maxRetries=0,
                    joins=pc.joins, scopeQuestion=scope_question,
                )
            except Exception as genErr:
                # 重试生成本身失败：抛原始执行错误（异常类型与消息不变，行为与旧实现
                # 一致），但必须留痕，否则「重试为什么也没救回来」在日志里无从查证
                # （对外仍只报首次错误）。这次生成**确实调了 LLM**：generateSql 把
                # 已消耗的 token 挂在 Nl2SqlError 上交回（它自己不落账），所以不仅要
                # 记下失败原因，还要把用量一并交出（核心约束 #3——失败路径也是计量路径）
                logger.warning("回灌重试的 SQL 生成失败: %s", genErr, exc_info=True)
                _attachRetryFailure(firstErr, "生成失败", genErr)
                _attachRetryGenTokens(firstErr, self._consumedTokens(genErr))
                raise firstErr
            try:
                data = await self._runQuery(pc, dto, retryResult.sql)
            except Exception as retryErr:
                # 重试后仍执行失败：抛原始执行错误（对外异常类型与消息保持旧实现口径，
                # 不动 API 层的状态映射），但必须把三件事带出去，不能只进一行日志：
                # ①这次重试生成已消耗的 token（调用方落账，核心约束 #3）；
                # ②二次失败详情（用户可见的步骤文案要给出两段原因，M7）；
                # ③重试 SQL —— 它是「重试为什么也没救回来」唯一的证据，此前无处可查
                logger.warning(
                    "回灌重试后仍执行失败: %s（重试 SQL: %s）",
                    retryErr,
                    _clipText(retryResult.sql or "", _RETRY_SQL_LOG_LIMIT),
                    exc_info=True,
                )
                _attachRetryFailure(firstErr, "后仍执行失败", retryErr)
                _attachRetryGenTokens(
                    firstErr,
                    (retryResult.promptTokens, retryResult.completionTokens),
                )
                raise firstErr
            return data, retryResult.sql, (retryResult.promptTokens, retryResult.completionTokens)

    async def _accountRetryGenUsage(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        exc: Exception,
        pc: _PipelineContext,
        outcome: _SqlOutcome,
    ) -> _RetryGenUsage | None:
        """把 `_runQueryWithRetry` 二次失败时随异常交回的「重试生成」用量落账。

        两种二次失败都覆盖：重试生成成功但重试执行又失败（用 `SqlResult` 的用量），
        以及重试生成自己就失败（用 `Nl2SqlError.tokens`）。两种情形都真的调了 LLM、
        都花了钱，必须写台账并计入总量（核心约束 #3 —— 失败路径也是计量路径）。
        返回用量增量供调用方累加；异常未携带用量（压根没走到重试，或该异常不带
        可计量信息）时返回 None，调用方无需判断。不改动入参。

        调用时机是「执行失败后、决定回退/上抛之前」（单步）或「隔离步骤时」（多步）：
        异常已在此收口，任何分支都不会重复落账。
        """
        pt, ct = _retryGenTokens(exc)
        if not (pt or ct):
            return None
        cfg = outcome.sqlConfig or pc.selected
        cost = self._costFor(cfg, pt, ct)
        await self._recordUsage(session, dto.sessionId, cfg, pt, ct, purpose="nl2sql")
        return _RetryGenUsage(tokens=pt + ct, cost=cost, modelName=cfg.model_name)

    @staticmethod
    def _columns(data: list[dict]) -> list[str]:
        return list(data[0].keys()) if data else []

    async def _chartStep(
        self,
        session: AsyncSession,
        dto: ChatRequest,
        pc: _PipelineContext,
        data: list[dict],
        intentChartType: ChartType | None = None,
    ) -> tuple[Any, dict, int, int]:
        """图表类型推荐 + option 生成（失败自动回退规则 option），有消耗时记录用量。

        优先级：客户端显式 dto.chartType > 意图抽取 intentChartType（3-3，"换成柱状图"）
        > 按数据形状自动推荐。返回 (chartType, option, promptTokens, completionTokens)。
        """
        columns = self._columns(data)
        chartType = (
            dto.chartType
            if dto.chartType is not None
            else intentChartType
            if intentChartType is not None
            else self._chart.recommendChartType(columns, data)
        )
        option, chartPt, chartCt = await self._chart.generateChartOption(
            chartType, columns, data, dto.question, pc.client, pc.selected,
        )
        await self._recordChartUsage(session, dto, pc.selected, chartPt, chartCt)
        return chartType, option, chartPt, chartCt

    async def _generateAnswer(
        self, session: AsyncSession, dto: ChatRequest, pc: _PipelineContext,
        data: list[dict], sql: str,
    ) -> tuple[Any, LlmConfig, tuple[int, int]]:
        """自然语言回答（失败时降级到最便宜可用模型重试一次）；用户明确选模型时跳过降级。"""
        answerResp, answerConfig, wasted = await self._callWithFallback(
            session, dto.sessionId, pc.configs, pc.selected, "answer",
            lambda cfg: self._llmFactory(cfg).complete(
                messages=[
                    LlmMessage(role="system", content=_ANSWER_SYSTEM_PROMPT),
                    LlmMessage(
                        role="user",
                        content=self._buildAnswerPrompt(
                            dto.question, sql, data, history=pc.contextPrompt,
                        ),
                    ),
                ],
                model=cfg.model_name,
            ),
            forced=pc.forcedModel,
        )
        return answerResp, answerConfig, wasted

    def _summarizeUsage(
        self, outcome: _SqlOutcome, chartPt: int, chartCt: int,
        answerResp: Any, answerConfig: LlmConfig, wastedAnswer: tuple[int, int],
        primary: LlmConfig,
    ) -> tuple[int, Decimal]:
        """汇总本轮全部 LLM 消耗（含降级前浪费），与 DB 审计行一致。返回 (tokens, cost)。"""
        total = (
            outcome.promptTokens + outcome.completionTokens + outcome.wasted[0] + outcome.wasted[1]
            + chartPt + chartCt
            + answerResp.promptTokens + answerResp.completionTokens + wastedAnswer[0] + wastedAnswer[1]
        )
        cost = self._costForSql(outcome, primary)
        cost += self._costFor(primary, chartPt, chartCt)
        cost += self._costFor(answerConfig, answerResp.promptTokens, answerResp.completionTokens)
        cost += self._costFor(primary, wastedAnswer[0], wastedAnswer[1])
        return total, cost

    def _costForSql(self, outcome: _SqlOutcome, primary: LlmConfig) -> Decimal:
        """SQL 阶段成本：捷径零消耗；两阶段按实际服务模型 + 主模型浪费分别计费。"""
        cost = Decimal("0")
        if outcome.sqlConfig is not None:
            # 4-1（feat-token-cache）：两阶段 cached_tokens 合并计入成本修正。
            cost += self._costFor(
                outcome.sqlConfig, outcome.promptTokens, outcome.completionTokens,
                cachedTokens=outcome.cachedTokens,
            )
        cost += self._costFor(primary, outcome.wasted[0], outcome.wasted[1])
        return cost

    async def _recordChartUsage(
        self, session: AsyncSession, dto: ChatRequest, config: Any, chartPt: int, chartCt: int,
    ) -> None:
        if chartPt + chartCt > 0:
            await self._recordUsage(session, dto.sessionId, config, chartPt, chartCt, purpose="chart")

    async def _recordAnswerUsage(
        self, session: AsyncSession, dto: ChatRequest, config: Any, resp: Any,
    ) -> None:
        await self._recordUsage(
            session, dto.sessionId, config,
            resp.promptTokens, resp.completionTokens, purpose="answer",
        )

    @staticmethod
    def _consumedTokens(exc: Exception) -> tuple[int, int]:
        """提取异常携带的已消耗 token；无法计量时返回 (0, 0)。

        委托 `llm_retry_policy.consumedTokens`（SSOT）：`Nl2SqlError.tokens` 之外
        还读「逃逸异常上补挂的累计用量」（M4）—— 否则多轮生成中途抛错时，
        前几轮已测得的 token 会静默消失。
        """
        return consumedTokens(exc)

    async def _recordUsage(
        self,
        session: AsyncSession,
        sessionId: str,
        config: Any,
        promptTokens: int,
        completionTokens: int,
        *,
        purpose: str,
        cachedTokens: int | None = None,
    ) -> None:
        # 4-1（feat-token-cache）：cachedTokens 透传进成本计算（_costFor 按差额计）。
        cost = self._costFor(
            config, promptTokens, completionTokens, cachedTokens=cachedTokens,
        )
        await self._tokenUsage.recordUsage(
            session,
            sessionId=sessionId,
            modelConfigId=config.id,
            modelName=config.model_name,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            cost=cost,
            purpose=purpose,
        )

    async def _recordDirectUsage(
        self,
        session: AsyncSession,
        sessionId: str,
        *,
        tokens_used: int,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost: float,
        model_name: str | None,
        purpose: str,
    ) -> None:
        """按已知计量写 token_usage（供 Agent/Tool 路径：模型配置未透传）。

        与 _recordUsage 的区别：ModelConfig 未透传到工具内部，无法用配置推算成本，
        直接落工具已计算的 total tokens + cost（modelConfigId=None）。tokens<=0
        （模板降级、无 LLM 调用）时跳过。审查 MEDIUM#2 修复。

        Phase 7 G2：拆分来源显式传 prompt/completion；未拆分来源（None）回退
        「全量计 prompt」，绝不让拆分量静默丢 0。
        """
        if tokens_used <= 0:
            return
        effective_prompt = (
            tokens_used if prompt_tokens is None else prompt_tokens
        )
        effective_completion = (
            0 if completion_tokens is None else completion_tokens
        )
        await self._tokenUsage.recordUsage(
            session,
            sessionId=sessionId,
            modelConfigId=None,
            modelName=model_name,
            promptTokens=effective_prompt,
            completionTokens=effective_completion,
            cost=Decimal(str(cost)),
            purpose=purpose,
        )

    @staticmethod
    def _costFor(
        config: Any,
        promptTokens: int,
        completionTokens: int,
        cachedTokens: int | None = None,
    ) -> Decimal:
        """按 model 单价计费。

        4-1（feat-token-cache，2026-09-28）：DeepSeek prompt cache 命中时
        cached_tokens 非零，对应部分不计 input 成本（按差额计费）。None 或 0
        → 全额按 prompt 计；cached >= prompt → input cost = 0。
        """
        if cachedTokens is not None and cachedTokens > 0:
            billablePrompt = max(0, promptTokens - cachedTokens)
        else:
            billablePrompt = promptTokens
        # 4-1：单价来自 ORM Numeric 列但在测试里传 float；显式 str() 走
        # Decimal 路径，避免与 Decimal * float 的 TypeError。
        inputCost = (
            Decimal(billablePrompt) * Decimal(str(config.cost_per_1k_input)) / Decimal(1000)
        )
        outputCost = (
            Decimal(completionTokens) * Decimal(str(config.cost_per_1k_output)) / Decimal(1000)
        )
        return inputCost + outputCost
