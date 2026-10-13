"""研究状态机的**相位执行体**与相位纯函数（feat-research-entry-ux-fixes Task 9 Step 0 搬迁）。

抽出动因：`research_agent_ports.py` 与 `research_agent_service.py` 双双抵达 800 行硬上限
（`Harness/rules/工程结构.md`），W5-b 需在两文件上各加数行，故先腾空间、零行为变化，单独提交。

承载两块：

1. **`ResearchAgentPhasesMixin`**：7 个相位执行体（`_stageIntent` / `_stageEsl` / `_stagePlan` /
   `_stageExecute` / `_stageHypothesis` / `_stageVerify` / `_stageReport`）—— 逐字从
   `research_agent_service.py` 搬入，语义一行未改；`ResearchAgentService` 以本 mixin 为基类，
   `_buildStages` 里的 `self._stageXxx` 经 MRO 解析到此处。
2. **相位纯函数**：`nextPhase` / `isDegraded` / `findingData`（Step 0 自 ports 搬入）与
   `buildOptions` / `stepSignal` / `singleStepPlan` / `normalizePlan` / `planQuestionWithFeedback`
   （Task 9 fix round 1 自 ports 搬入，理由同为 ports 内部零调用）；连同 `PLAN_FEEDBACK_HEADER`。

依赖方向**单向**：service → phases → ports（phases 只读 ports 的词汇与构件；ports 不导入本模块）。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchRunState
from app.services.enterprise_semantic_layer import EmptyResearchScopeError
from app.services.hypothesis_service import Hypothesis
from app.services.research_finding_aggregator import (
    aggregateRows,
    buildEnhancedClaim,
)
from app.services.learning.prompt_fence import neutralizeFence
from app.services.research_agent_execution import (
    adapterFor,
    depsForSession,
    executedStepCount,
    finalizeStepResults,
    runStep,
)
from app.services.research_evidence_summary import (
    EvidenceSummary,
    trySummarizeEvidence,
)
from app.services.research_step_result import persistStepResult
from app.services.research_hypothesis_adapter import rewriteHypothesisSqlWithHint
from app.services.research_hypothesis_test import (
    HypothesisVerdict,
    evaluateHypothesis,
)
from app.services.research_agent_ports import (
    ACTION_MODIFY,
    ACTION_REJECT,
    CHECKPOINT_HYPOTHESIS,
    CHECKPOINT_INTENT,
    CHECKPOINT_LOW_CONFIDENCE,
    CHECKPOINT_PLANNING,
    CHECKPOINT_RUNTIME_DYNAMIC,
    CONFIDENCE_ROUND_DIGITS,
    DEFAULT_MODE,
    EVENT_ESL,
    EVENT_FINDING,
    EVENT_HYPOTHESIS,
    EVENT_INTENT,
    EVENT_PLAN,
    EVENT_REPORT,
    FINDING_ROWS_KEY,
    MAX_FINDING_ROWS,
    MAX_REPLAN_PER_TURN,
    MAX_RESEARCH_DATA_STEPS,
    OPT_ABORT_PHASE,
    OPT_REPLAN_COUNT,
    OPT_RESUME_PHASE,
    OPT_SIGNAL,
    PHASE_ESL,
    PHASE_EXECUTE,
    PHASE_PLAN,
    PHASE_REPORT,
    PHASE_VERIFY,
    PURPOSE_HYPOTHESIS,
    PURPOSE_PLAN,
    PURPOSE_REPORT,
    SIGNAL_EMPTY_SCOPE,
    SIGNAL_FIXED_HYPOTHESIS,
    SIGNAL_FIXED_PLAN,
    SIGNAL_FIXED_SCOPE,
    SIGNAL_LOW_CONFIDENCE_STEP,
    SIGNAL_SQL_VALIDATION_FAILED,
    STEP_RESULT_RAW_ROWS_KEY,
    VERIFY_FAIL_FACTOR,
    VERIFY_OK_FACTOR,
    Emit,
    MeteredClient,
    Pause,
    emitEvent,
    nextPhaseForPhase,
)
from app.services.research_agent_stages import (
    ambiguityPrompt,
    candidateConfidence,
    clientModelName,
    hypothesisPrompt,
    meteringModelName,
    rebuildState,
    selectedHypotheses,
)

logger = logging.getLogger(__name__)


class ResearchAgentPhasesMixin:
    """7 个相位执行体（各 < 50 行；返回 Pause 表示暂停，None 表示继续）。"""

    # ------------------------------------------------------------------
    # 相位实现（各 < 50 行；返回 Pause 表示暂停，None 表示继续）
    # ------------------------------------------------------------------

    async def _stageIntent(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[1] 意图分类（纯规则，零 LLM / 零 DB）→ 决定研究 mode。"""
        result = self._intent.classifyResult(state["question"])
        state["intent"] = {"intent": result.intent.value, "mode": state.get("mode")}
        await emitEvent(emit, EVENT_INTENT, dict(state["intent"]))
        return None

    async def _stageEsl(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[2][3] 三臂拆解 → 动态冲突点（先于固定 #1）→ 固定 #1 范围确认。"""
        try:
            extraction = await self._esl.extract(state["question"])
        except EmptyResearchScopeError as exc:
            logger.warning("ESL 三臂全空，转范围确认强制改写: session=%s err=%s", sessionId, exc)
            emptyArms = {"businessObjects": [], "metrics": [], "knowledge": [], "conflicts": []}
            return (
                CHECKPOINT_INTENT,
                buildOptions(
                    signal=SIGNAL_EMPTY_SCOPE, arms=emptyArms, resumePhase=PHASE_ESL
                ),
                "三臂检索全空：请改写问题后继续",
            )
        arms = asdict(extraction)
        state["esl"] = arms
        await emitEvent(emit, EVENT_ESL, arms)
        if self._autoConfirm:
            return None
        if extraction.conflicts:
            # 动态点先于固定 #1：冲突未决时范围确认没有意义（且 brief 的契约测试要求
            # 第一个 pending checkpoint 就是 runtime_dynamic）。
            kinds = sorted({c.kind for c in extraction.conflicts})
            return (
                CHECKPOINT_RUNTIME_DYNAMIC,
                buildOptions(
                    signal=kinds[0],
                    arms=arms,
                    resumePhase=PHASE_PLAN,
                    conflicts=arms["conflicts"],
                ),
                ambiguityPrompt(arms["conflicts"]),
            )
        return (
            CHECKPOINT_INTENT,
            buildOptions(signal=SIGNAL_FIXED_SCOPE, arms=arms, resumePhase=PHASE_PLAN),
            "三臂是否齐全？",
        )

    async def _stagePlan(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[4][5] 多步计划（LLM，计量）→ 固定 #2 计划确认。

        `state["replan"]`（Task 6.5-4 + 2026-10-11 裁定）：计划 checkpoint 的 modify 恢复。
        此时把用户 choice 回灌 planner 重跑出**新计划**，并**重新暂停**在同一检查点，
        让用户看得到改后的计划。迭代次数由 `resumeTurn` 的 `MAX_REPLAN_PER_TURN` 兜底
        （达上限后第 N+1 次 modify 按 confirm 推进），故不存在「改了又改」的死循环。
        """
        replan = bool(state.pop("replan", False))
        replan_count = int(state.get(OPT_REPLAN_COUNT) or 0)
        if replan:
            logger.info("第 %s 次 replan", replan_count)
        question = (
            planQuestionWithFeedback(state["question"], state.get("choice"))
            if replan
            else state["question"]
        )
        # 顺序承重：先组合 modify 反馈、再叠加 mode 指引（两者都要保留，不能互相覆盖）。
        # 注入的 `plannedQuestion` 只当 planner 入参，**不**等于「只送 planner」：planner 会把
        # 入参记成 `original_question`，`normalizePlan` 又抄进 `plan["originalQuestion"]` 并随
        # 计划检查点 options(JSONB) **落库**（research 链路无读者，当前是惰性字段，但仍是把
        # prompt 脚手架写进存储）。故两处都改回**未注入**的 `question`：① 下面的回落
        # `singleStepPlan(question)`；② 归一化后的 `originalQuestion`（见下一处注释）。
        plannedQuestion = planQuestionForMode(question, state.get("mode") or DEFAULT_MODE)
        client = await self._llmClient(session, state=state, emit=emit, sessionId=sessionId)
        result = await self._planner.plan(plannedQuestion, client, clientModelName(client))
        rawPlan = getattr(result, "plan", result)
        plan = normalizePlan(rawPlan) if rawPlan is not None else singleStepPlan(question)
        # `normalizePlan` 是模块级纯函数，看不到未注入值 ⇒ 只能在调用点用**新 dict** 覆盖
        # （不就地改任何共享对象；`singleStepPlan` 分支的 `originalQuestion` 本已等于 question）。
        plan = {**plan, "originalQuestion": question}
        state["plan"] = plan
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_PLAN,
            promptTokens=int(getattr(result, "prompt_tokens", 0) or 0),
            completionTokens=int(getattr(result, "completion_tokens", 0) or 0),
            modelName=clientModelName(client),
        )
        await emitEvent(emit, EVENT_PLAN, {"steps": plan["steps"]})
        if self._autoConfirm:
            return None
        return (
            CHECKPOINT_PLANNING,
            buildOptions(
                signal=SIGNAL_FIXED_PLAN, plan=plan, arms=state.get("esl"),
                resumePhase=PHASE_EXECUTE,
                **{OPT_REPLAN_COUNT: state.get(OPT_REPLAN_COUNT, 0)},
            ),
            "计划是否确认？",
        )

    async def _stageExecute(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[6] 逐步只读执行 + 出图；步失败/空数据 → 动态 low_confidence_step 点。"""
        steps = [
            s for s in (state.get("plan") or {}).get("steps", []) if not s.get("aggregation_only")
        ]
        if not steps:  # N1 急停：无数据步 ⇒ 终态失败（`_guardedRun` 转 research.error + failed），不出报告
            raise RuntimeError("研究计划无数据步（aggregation_only 过滤后为空），本 turn 终止")
        results: list[dict[str, Any]] = list(state.get("stepResults") or [])
        startIndex = int(state.get("resumeStepIndex") or 0)
        # Task 4 契约：执行失败会 rollback 注入的 session，先固化本 turn 已写入的状态行
        await session.commit()
        deps = await depsForSession(session, sessionId, self._exec)  # Task 13e：业务库源按次送达
        for step in steps:
            if int(step.get("index", 0)) < startIndex:
                continue
            result = await runStep(
                session, step, sessionId=sessionId, state=state, emit=emit, deps=deps
            )
            # Task 6：摘除内部载体（驱动原始行）后落库；每步一次 commit —— 失败步的
            # rollbackQuietly 会回滚未提交的行，故落库必须紧贴本步之后。
            raw_rows = result.pop(STEP_RESULT_RAW_ROWS_KEY, [])
            results.append(result)
            state["stepResults"] = results
            await persistStepResult(
                session, state=state, step=step, result=result, raw_rows=raw_rows
            )
            if not result["error"] and result["rowCount"] > 0:
                continue
            if self._autoConfirm:
                continue
            return (
                CHECKPOINT_LOW_CONFIDENCE,
                buildOptions(
                    signal=stepSignal(result["error"]),
                    plan=state.get("plan"),
                    arms=state.get("esl"),
                    stepResults=results, stepsExecuted=executedStepCount(results),
                    stepIndex=result["index"],
                    error=result["error"] or "该步骤无数据返回",
                    resumePhase=PHASE_EXECUTE,
                    abortPhase=PHASE_REPORT,
                    nextStepIndex=result["index"] + 1,
                ),
                "步骤失败，跳过还是终止？",
            )
        finalizeStepResults(state, results)  # N1'：全部步失败 ⇒ 急停；否则写回步数（degraded 依据）
        return None

    async def _stageHypothesis(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[7][8] 假设生成（LLM，计量）→ 固定 #3 假设挑选。"""
        candidates = await self._generateCandidates(
            session, sessionId=sessionId, state=state, emit=emit
        )
        state["hypotheses"] = candidates
        await emitEvent(emit, EVENT_HYPOTHESIS, {"candidates": candidates})
        if self._autoConfirm:
            return None
        return (
            CHECKPOINT_HYPOTHESIS,
            buildOptions(
                signal=SIGNAL_FIXED_HYPOTHESIS,
                candidates=candidates,
                arms=state.get("esl"), stepsExecuted=executedStepCount(state.get("stepResults") or []),
                resumePhase=PHASE_VERIFY,
            ),
            hypothesisPrompt(candidates),
        )

    async def _stageVerify(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[9] 逐条验证假设并落 finding（confidence = 候选分 × 0.9 / 0.3）。

        Task 5（D16/D18）：另按「数据支持度」判 ``verdict``（落 ``supporting_data["verdict"]``
        与 ``state["findings"][i]``）；用户可见的 ``verified`` 改为 ``verdict == SUPPORTED``
        （不是「SQL 跑通」）。置信度公式不变（D17，仍由执行是否成功驱动因子）。
        """
        findings: list[dict[str, Any]] = []
        # Task 13e：验证 SQL 与执行步走同一条业务库路径（假设 SQL 也是业务 SQL）
        deps = await depsForSession(session, sessionId, self._exec)
        adapter = adapterFor(deps.datasource)
        for idx, candidate in enumerate(selectedHypotheses(state)):
            await session.commit()  # Task 4 契约：runVerification 失败会 rollback 本 session
            # 2026-10-08 复盘 3：把 dialectArgs 透传给 runVerification，让假设 SQL
            # 也走 checkSqlDialect 守卫（plan 步的 runStep 早已有，假设链路缺）
            outcome = await self._runner.runVerification(
                session, Hypothesis(**candidate), adapter=adapter, dialectArgs=deps.dialectArgs
            )
            # 2026-10-08 A2：拦截到方言违规时，把违规原因喂回 LLM 单次重写 SQL，再跑一次。
            # 硬限 1 次（避免 runVerification docstring 警告的"反复重写"反模式）；
            # rewrite 失败 / 重写后仍违规 → 用原 error 落库，confidence 走 fail 因子。
            # 不可变更新：用 spread 替换 state["hypotheses"][idx]（state 是工作流容器，
            # 替换 slot 是契约允许的；不动 dict 本身 —— 不污染后续轮 / retry）。
            if outcome.get("error") and outcome["error"].startswith("SQL 方言违规"):
                new_sql = await self._rewriteVerificationSqlForDialect(
                    session, sessionId=sessionId, state=state, emit=emit,
                    originalSql=candidate.get("verificationSql") or "",
                    hint=outcome["error"].removeprefix("SQL 方言违规: "),
                    datasourceType=(deps.dialectArgs or {}).get("datasourceType", ""),
                )
                if new_sql:
                    candidate = {**candidate, "verificationSql": new_sql}
                    state["hypotheses"][idx] = candidate
                    await session.commit()
                    outcome = await self._runner.runVerification(
                        session, Hypothesis(**candidate),
                        adapter=adapter, dialectArgs=deps.dialectArgs,
                    )
            # D17：置信度公式保留（候选分 × 0.9 / 0.3），因子仍由「验证是否跑通」驱动；
            # verdict 只作为**新增字段**，不改公式（否则候选之间的排序信息全丢）。
            executionSucceeded = outcome.get("error") is None
            confidence = round(
                candidateConfidence(state, candidate)
                * (VERIFY_OK_FACTOR if executionSucceeded else VERIFY_FAIL_FACTOR),
                CONFIDENCE_ROUND_DIGITS,
            )
            # D16/D18/D19：数据支持度判定 —— 与「执行是否成功」正交。证据摘要从验证行派生
            # （出图路径包装：算不出来只降级为 empty，绝不把单步升级成整 turn 失败）。
            rows = list(outcome.get("rows") or [])
            summary = trySummarizeEvidence(rows) or EvidenceSummary(empty=True)
            test = evaluateHypothesis(
                candidate, summary, rows, execution_error=outcome.get("error")
            )
            verdict = test.verdict
            # D18/P2：用户可见的 verified ⇔ **数据支持**（不再等价于「SQL 跑通」）。
            verified = verdict is HypothesisVerdict.SUPPORTED
            enhancedClaim, statsObj = self._composeVerifyEnhancement(outcome, candidate)
            # D19 接线：扩展 supportingData 字典本身（saveFinding 只认 `stats=` 这个具名
            # kwarg，别的新键不认）。evidence 已是 JSON 原生 ⇒ _coerceJsonSafe 全程 no-op。
            supportingData = {
                **findingData(outcome),
                "evidence": test.evidence_summary,
                "verdict": verdict.value,
            }
            # M1：`verified` 是 `verdict == supported` 的**定义性派生**（上面一行赋值），
            # `supportingData["verdict"]` 又是同一个 `verdict` ⇒ 「verified 与 verdict 相矛盾」
            # 恒不可达，故不设运行时守卫。
            row = await self._sessions.saveFinding(
                session,
                sessionId=sessionId,
                turnId=turnId,
                claimText=candidate["statement"],
                supportingSql=candidate.get("verificationSql"),
                supportingData=supportingData,
                confidence=confidence,
                enhancedClaim=enhancedClaim,
                stats=statsObj.asdict() if statsObj else None,
            )
            findings.append(
                {
                    "findingId": str(row.id),
                    "claim": candidate["statement"],
                    "confidence": confidence,
                    "verified": verified,
                    # D16 补：新增键安全（前端把载荷当 Record<string, unknown>）；不改/删旧键。
                    "verdict": verdict.value,
                    "executionSucceeded": executionSucceeded,
                    "limitations": test.limitations,
                }
            )
            await emitEvent(emit, EVENT_FINDING, findings[-1])
        state["findings"] = findings
        return None

    def _composeVerifyEnhancement(
        self, outcome: dict[str, Any], candidate: dict[str, Any],
    ) -> tuple[str | None, "FindingStats | None"]:
        """对 verify 阶段的 rows 做聚合，返回 (enhancedClaim, statsObj)。

        无维度 / 空 rows → (None, None)，触发 saveFinding 回落原 hypothesis。
        """
        rowsForStats = list(outcome.get("rows") or [])
        statsObj = aggregateRows(rowsForStats) if rowsForStats else None
        enhancedClaim = (
            buildEnhancedClaim(statsObj, originalClaim=candidate["statement"])
            if statsObj and statsObj.rowCount > 0 and statsObj.topContributors
            else None
        )
        return enhancedClaim, statsObj

    async def _rewriteVerificationSqlForDialect(
        self, session: AsyncSession, *, sessionId: uuid.UUID, state: dict[str, Any],
        emit: Emit | None, originalSql: str, hint: str, datasourceType: str,
    ) -> str | None:
        """把方言违规 hint 喂回 LLM，让其重写 verificationSql。

        A2 helper：与 chat 侧 retry-with-feedback 同思路，但**单次硬限**（避免反复重写）。
        失败一律返 None（caller 走 fallback：用原 error 字符串落库，不影响主流程）。
        用量走 ``PURPOSE_HYPOTHESIS`` 维度（与 `_generateCandidates` 同款，不引入新维度）。
        """
        if not originalSql or not hint:
            return None
        client = await self._llmClient(session, state=state, emit=emit, sessionId=sessionId)
        if client is None:
            return None
        metered = MeteredClient(client)
        new_sql = await rewriteHypothesisSqlWithHint(
            metered, originalSql, hint, datasourceType,
        )
        if metered.promptTokens or metered.completionTokens:
            # 一次 LLM 调用 = 一次计量；空调用不浪费一行 session_token_usage
            await self._recordUsage(
                session,
                sessionId=sessionId,
                purpose=PURPOSE_HYPOTHESIS,
                promptTokens=metered.promptTokens,
                completionTokens=metered.completionTokens,
                modelName=meteringModelName(client, metered),
                cachedTokens=metered.cachedTokens,
            )
        return new_sql

    async def _stageReport(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[10][11][12] 报告装配（reporter，Task 6 为真实 ReportPlanner）+ 归档。

        LLM 文本块经 `MeteredClient` 计量（核心约束 #3）：一次 compose 会逐块调用，
        MeteredClient 累加后此处一次性记 `purpose=research_report`；reporter 自身不记账
        （避免双计）。归档（version=max+1、旧版 superseded）留在本方法，reporter 只装配。
        """
        client = await self._llmClient(session, state=state, emit=emit, sessionId=sessionId)
        metered = MeteredClient(client) if client is not None else None
        try:
            payload, renderedMd = await self._reporter.compose(
                session,
                sessionId=sessionId,
                turnId=turnId,
                mode=state.get("mode") or DEFAULT_MODE,
                llmClient=metered,
            )
        finally:
            # 记账必须在 finally：compose 逐块调 LLM，抛错时已消耗的 token 若不落账
            # 就成了永久盲区（核心约束 #3）。recordUsageQuietly 从不抛（零消耗跳过、
            # 其余异常 catch + log）⇒ 不会顶掉 compose 的原始异常。
            if metered is not None:
                await self._recordUsage(
                    session,
                    sessionId=sessionId,
                    purpose=PURPOSE_REPORT,
                    promptTokens=metered.promptTokens,
                    completionTokens=metered.completionTokens,
                    modelName=meteringModelName(client, metered),
                    cachedTokens=metered.cachedTokens,
                )
        report = await self._sessions.publishReport(
            session, sessionId=sessionId, payload=payload, renderedMd=renderedMd
        )
        state["reportId"] = str(report.id)
        state["reportVersion"] = report.version
        await emitEvent(
            emit,
            EVENT_REPORT,
            {
                "reportId": str(report.id),
                "version": report.version,
                "degraded": isDegraded(state),
            },
        )
        return None



# ---------------------------------------------------------------------------
# 无状态构件：options / 信号 / 计划归一化 / 计划反馈回灌
# （Task 9 fix round 1 从 research_agent_ports.py 搬入：ports 内部零调用，搬迁后
#   ports 不再导入本模块，依赖方向仍是 service → phases → ports。）
# ---------------------------------------------------------------------------

PLAN_FEEDBACK_HEADER = "[用户修改要求]"

# --- mode 计划指引（Task 13）-------------------------------------------------
# 报告章节早已按 mode 差异化（`report_planner._MODE_SECTIONS`），但**计划**链路对 mode
# 零消费 —— 三 mode 拆出的步一模一样。这里按 mode 给 planner 一段指引，经「问题文本注入」
# 进入（与 `planQuestionWithFeedback` 同款：`StepQueryPlanner.plan` 没有 mode 形参，也不该有）。
# 键集合与 `research_schemas.ResearchMode` 的取值逐一对应（那边是 Literal，无常量可引；
# 默认值走 `DEFAULT_MODE`，不硬编码 "research" 字面量）。
#
# 措辞两条硬约束（Task 13 fix1，之前违反过，见 `test_guidance_has_no_sequencing_...` 守卫）：
# ① 不得指示拆分「一条 SQL 能算完的对比/汇总」（planner 系统提示的独立性子问题规则）；
# ② 不得暗示**步间依赖**（`下钻` / `逐层` / `收窄` / `随后` 之类）—— 执行器
#    `generateStepSql` 只用本步 `sub_question` 生成 SQL，前序步的行结果**从不**喂给下一步。
# 故三 mode 只在「覆盖哪些**互相独立**的口径/分面」上区分，不在「先后顺序」上区分。
_MODE_GUIDANCE: dict[str, str] = {
    DEFAULT_MODE: (
        "【研究指引·探查】覆盖总体口径与量级（总量/趋势），并分别给出若干**互相独立**的分面视角"
        "（如按品类、区域、供应商各看同一口径）；各分面共用同一时间窗与过滤条件、彼此不依赖，"
        "每步只回答一个子问题，且能由一条 SQL 一次算完的口径不要拆。"
    ),
    "attribution": (
        "【研究指引·归因】围绕同一基线（上期/目标/同类）分别量化若干**互相独立**的候选解释维度"
        "（如价格、数量、区域、供应商各自偏离多少），每个候选维度各用一条 SQL 独立对照基线、"
        "彼此不依赖，便于报告比较哪个偏离最大；每步只回答一个子问题，"
        "且能由一条 SQL 一次算完的口径不要拆。"
    ),
    "compare": (
        "【研究指引·对比】每个对比对象都取**同一口径**（同一时间窗、同一过滤条件、同一聚合方式），"
        "各对象各自独立取数、互不依赖（任一侧缺失即该维度不成立），并分别给出各对象的量值与构成拆分；"
        "**若一条 SQL 即可算出全部对比，就不要拆分**，每步只回答一个子问题。"
    ),
}


def planQuestionForMode(question: str, mode: str) -> str:
    """把 mode 对应的研究指引拼进**规划问题**（Task 13）。

    `StepQueryPlanner.plan(question, client, model_name)` 没有 mode 形参（也不该有），
    故指引只能经问题文本进入 —— 与 `planQuestionWithFeedback` 的反馈回灌同款。

    未命中（未知 / 空 mode）⇒ **逐字**原样返回并记 debug，**不抛**：规划阶段不该因
    一个 mode 取值把整轮研究打断（`compose` 对未知 mode 抛 `ValueError` 是**报告阶段**
    的策略，不照搬过来）。

    指引是**模块常量**（非用户内容）⇒ 无需 `neutralizeFence`（用户内容在 `question`
    一侧，已由既有链路中和）。返回值是新字符串，入参不被改写。
    """
    guidance = _MODE_GUIDANCE.get(mode)
    if not guidance:
        logger.debug("未知研究 mode=%r，规划问题不注入 mode 指引", mode)
        return question
    return f"{question}\n\n{guidance}"


def buildOptions(*, signal: str, resumePhase: str, **payload: Any) -> dict[str, Any]:
    """checkpoint options：信号 + 恢复相位 + 语义载荷（前端卡片直接渲染）。"""
    return {
        OPT_SIGNAL: signal,
        OPT_RESUME_PHASE: resumePhase,
        **{key: value for key, value in payload.items() if value is not None},
    }


def stepSignal(error: str | None) -> str:
    """步失败**checkpoint signal 词汇**：SQL Guard 拒绝单列，其余归 low_confidence_step（设计 §4.8）。

    注意：本函数产出 **checkpoint signal 词汇**（落 options["signal"] / phase 白名单），
    不是 error code —— `research.error` 事件的 code 走 `stepErrorCode`，勿混用。
    """
    if error and error.startswith(SIGNAL_SQL_VALIDATION_FAILED):
        return SIGNAL_SQL_VALIDATION_FAILED
    return SIGNAL_LOW_CONFIDENCE_STEP


def singleStepPlan(question: str) -> dict[str, Any]:
    """planner 判为单步（`plan=None`）时的回落计划：一句一问，单步执行，复用既有执行链路。

    与 chat 同口径，字段与 `normalizePlan` 输出契约一致（N1：此前 `None` 归一成
    `{"steps": []}` ⇒ 零 SQL 仍出报告）。
    """
    return {
        "steps": [{"index": 0, "description": question, "sub_question": question,
                   "sql": None, "aggregation_only": False}],
        "aggregationHint": "",
        "originalQuestion": question,
    }


def normalizePlan(plan: Any) -> dict[str, Any]:
    """把 planner 产物归一成可落 JSONB 的 dict（兼容 StepPlanResult.plan 为 None）。"""
    if plan is None:
        logger.warning("计划器返回空计划（单步或拆分失败），按无步计划继续")
        return {"steps": [], "aggregationHint": "", "originalQuestion": ""}
    steps = [
        {
            "index": int(getattr(raw, "index", position)),
            "description": str(getattr(raw, "description", "") or ""),
            "sub_question": str(getattr(raw, "sub_question", "") or ""),
            "sql": getattr(raw, "sql", None),
            "aggregation_only": bool(getattr(raw, "aggregation_only", False)),
        }
        for position, raw in enumerate(getattr(plan, "steps", None) or [])
    ]
    return {
        "steps": _capDataSteps(steps),
        "aggregationHint": str(getattr(plan, "aggregation_hint", "") or ""),
        "originalQuestion": str(getattr(plan, "original_question", "") or ""),
    }


def _capDataSteps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """数据步硬上限（N2）：LLM 计划步数不受控 ⇒ 执行面（每步一次 NL2SQL + 一次 SQL）无界。

    只截数据步，`aggregation_only` 汇总步不占额度、原样保留（与 chat 同口径）。
    截断必须**有声**：日志留痕，且计划检查点会把截断后的计划原样展示给用户。
    """
    dataPositions = [i for i, s in enumerate(steps) if not s.get("aggregation_only")]
    if len(dataPositions) <= MAX_RESEARCH_DATA_STEPS:
        return steps
    keep = set(dataPositions[:MAX_RESEARCH_DATA_STEPS]) | {
        i for i, s in enumerate(steps) if s.get("aggregation_only")
    }
    logger.warning(
        "研究计划数据步超上限 %s，截断 %s 步（保留前 %s 个数据步 + 全部汇总步）",
        MAX_RESEARCH_DATA_STEPS,
        len(dataPositions) - MAX_RESEARCH_DATA_STEPS,
        MAX_RESEARCH_DATA_STEPS,
    )
    return [s for i, s in enumerate(steps) if i in keep]


def planQuestionWithFeedback(question: str, feedback: dict[str, Any] | None) -> str:
    """把计划 checkpoint 的 modify 反馈回灌成 planner 输入（Task 6.5-4）。

    真实 `StepQueryPlanner.plan(question, client, model_name)` 没有反馈参数
    （Step 0 实测），故反馈只能经**问题文本**进入 —— 与 chat 注入 scopeQuestion /
    priorState 同款。反馈是用户内容，先经 `neutralizeFence` 打断围栏标签，避免其
    提前闭合 `<user_content>` 被当成指令执行。
    """
    if not feedback:
        return question
    payload = neutralizeFence(json.dumps(feedback, ensure_ascii=False, sort_keys=True))
    return f"{question}\n\n{PLAN_FEEDBACK_HEADER} {payload}"


def resumeState(runState: ResearchRunState, checkpoint: Any, choice: dict[str, Any] | None) -> dict[str, Any]:
    """重建恢复态（W5）：modelId 由 runState 携带（turn 启动时从会话行快照）。

    checkpoint 载荷不带 modelId（模型不是落库状态）；`rebuildState` 从 runState 补回，
    故此处只覆写 `choice`，`state` 形状与恢复前逐字一致。
    """
    state = rebuildState(runState, checkpoint)
    state["choice"] = choice or {}
    return state


def nextPhase(checkpoint: Any, action: str) -> str:
    """决策后回到哪个相位；动态点 reject 时走 `abortPhase`（终止到出报告）。"""
    options = checkpoint.options or {}
    if action == ACTION_REJECT and options.get(OPT_ABORT_PHASE):
        return str(options[OPT_ABORT_PHASE])
    return nextPhaseForPhase(checkpoint.phase, options)


def rewrittenQuestion(choice: dict[str, Any] | None) -> str:
    """用户改写后的问题（空串 = 未改写）。**唯一推导点**：端点的决策与落库的 turn 共用它。"""
    return str((choice or {}).get("question") or "").strip()


@dataclass(frozen=True)
class ResumeDecision:
    """续跑决策：`phase` = 从哪个相位续跑；`isReplan` = 是否走「重跑计划」分支。"""

    phase: str
    isReplan: bool


def decideResume(checkpoint: Any, action: str, choice: dict[str, Any] | None) -> ResumeDecision:
    """checkpoint 决策 → 续跑相位。**端点与 `resumeTurn` 共用这一份**（M3）。

    判定顺序与 `resumeTurn` 历史实现逐字一致：改写 > replan > （reject → abort，否则
    `nextPhaseForPhase`）。判定键 `replanCount` 读**已落库**的 checkpoint.options
    （`_stagePlan` 每次 replan 都把它写回新 checkpoint 的 options）。
    """
    if rewrittenQuestion(choice):
        return ResumeDecision(phase=PHASE_ESL, isReplan=False)
    if action == ACTION_MODIFY and checkpoint.phase == CHECKPOINT_PLANNING:
        replanCount = int((checkpoint.options or {}).get(OPT_REPLAN_COUNT) or 0)
        if replanCount < MAX_REPLAN_PER_TURN:
            return ResumeDecision(phase=PHASE_PLAN, isReplan=True)
        logger.info(
            "replan 上限已达 %s，按 confirm 推进: session=%s",
            MAX_REPLAN_PER_TURN, checkpoint.session_id,
        )
    return ResumeDecision(phase=nextPhase(checkpoint, action), isReplan=False)


def isDegraded(state: dict[str, Any]) -> bool:
    """降级口径 SSOT（Task 14 / N1）：无可用 LLM，或**本 turn 零个数据步被执行**（不判行数）。

    步数读 `state["stepsExecuted"]`（写回 + 经 checkpoint 载荷跨恢复轮还原，缺键按 0 —— 即
    宁可在未知路径上标降级）；「有步执行但 0 行」是合法答案，既有机制已落成动态点，不在此叠一层。"""
    return bool(state.get("llmUnavailable")) or not state.get("stepsExecuted")


def findingData(outcome: dict[str, Any]) -> dict[str, Any]:
    """finding.supporting_data 契约（Task 5 写 / Task 6 报告读）。

    行数据必须落库：步结果只活在内存 state 里，`research_finding.supporting_data`
    是报告 chart/table 块的**唯一**持久化数据源（Task 6 §4.7 偏差）。行数超
    `MAX_FINDING_ROWS` 时截断并 warning（不静默丢数据）。
    """
    rows = list(outcome.get("rows") or [])
    if len(rows) > MAX_FINDING_ROWS:
        logger.warning(
            "验证结果行数超上限，报告数据截断: rows=%s limit=%s", len(rows), MAX_FINDING_ROWS
        )
    return {
        "rowCount": len(rows),
        "error": outcome.get("error"),
        FINDING_ROWS_KEY: rows[:MAX_FINDING_ROWS],
    }
