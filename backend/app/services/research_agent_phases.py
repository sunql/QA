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
from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.enterprise_semantic_layer import EmptyResearchScopeError
from app.services.hypothesis_service import Hypothesis
from app.services.learning.prompt_fence import neutralizeFence
from app.services.research_agent_execution import (
    adapterFor,
    depsForSession,
    executedStepCount,
    finalizeStepResults,
    runStep,
)
from app.services.research_agent_ports import (
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
    OPT_ABORT_PHASE,
    OPT_RESUME_PHASE,
    OPT_SIGNAL,
    PHASE_ESL,
    PHASE_EXECUTE,
    PHASE_PLAN,
    PHASE_REPORT,
    PHASE_VERIFY,
    PURPOSE_PLAN,
    PURPOSE_REPORT,
    SIGNAL_EMPTY_SCOPE,
    SIGNAL_FIXED_HYPOTHESIS,
    SIGNAL_FIXED_PLAN,
    SIGNAL_FIXED_SCOPE,
    SIGNAL_LOW_CONFIDENCE_STEP,
    SIGNAL_SQL_VALIDATION_FAILED,
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
    eslClasses,
    hypothesisPrompt,
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

        `state["replan"]`（Task 6.5-4）：计划 checkpoint 的 modify 恢复。此时把用户
        choice 回灌 planner 重跑出**新计划**，且**不再暂停**在同一检查点（直接进
        execute）——否则用户会陷入「改了又改」的死循环。
        """
        replan = bool(state.pop("replan", False))
        question = (
            planQuestionWithFeedback(state["question"], state.get("choice"))
            if replan
            else state["question"]
        )
        client = await self._llmClient(session, state=state, emit=emit, sessionId=sessionId)
        result = await self._planner.plan(question, eslClasses(state), client, clientModelName(client))
        rawPlan = getattr(result, "plan", result)
        plan = normalizePlan(rawPlan) if rawPlan is not None else singleStepPlan(question)
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
        if replan or self._autoConfirm:
            return None
        return (
            CHECKPOINT_PLANNING,
            buildOptions(
                signal=SIGNAL_FIXED_PLAN, plan=plan, arms=state.get("esl"), resumePhase=PHASE_EXECUTE
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
            results.append(result)
            state["stepResults"] = results
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
        """[9] 逐条验证假设并落 finding（confidence = 候选分 × 0.9 / 0.3）。"""
        findings: list[dict[str, Any]] = []
        # Task 13e：验证 SQL 与执行步走同一条业务库路径（假设 SQL 也是业务 SQL）
        adapter = adapterFor((await depsForSession(session, sessionId, self._exec)).datasource)
        for candidate in selectedHypotheses(state):
            await session.commit()  # Task 4 契约：runVerification 失败会 rollback 本 session
            outcome = await self._runner.runVerification(
                session, Hypothesis(**candidate), adapter=adapter
            )
            verified = outcome.get("error") is None
            confidence = round(
                candidateConfidence(state, candidate)
                * (VERIFY_OK_FACTOR if verified else VERIFY_FAIL_FACTOR),
                CONFIDENCE_ROUND_DIGITS,
            )
            row = await self._sessions.saveFinding(
                session,
                sessionId=sessionId,
                turnId=turnId,
                claimText=candidate["statement"],
                supportingSql=candidate.get("verificationSql"),
                supportingData=findingData(outcome),
                confidence=confidence,
            )
            findings.append(
                {
                    "findingId": str(row.id),
                    "claim": candidate["statement"],
                    "confidence": confidence,
                    "verified": verified,
                }
            )
            await emitEvent(emit, EVENT_FINDING, findings[-1])
        state["findings"] = findings
        return None

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
        payload, renderedMd = await self._reporter.compose(
            session,
            sessionId=sessionId,
            turnId=turnId,
            mode=state.get("mode") or DEFAULT_MODE,
            llmClient=metered,
        )
        if metered is not None:
            await self._recordUsage(
                session,
                sessionId=sessionId,
                purpose=PURPOSE_REPORT,
                promptTokens=metered.promptTokens,
                completionTokens=metered.completionTokens,
                modelName=metered.modelName,
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
        "steps": steps,
        "aggregationHint": str(getattr(plan, "aggregation_hint", "") or ""),
        "originalQuestion": str(getattr(plan, "original_question", "") or ""),
    }


def planQuestionWithFeedback(question: str, feedback: dict[str, Any] | None) -> str:
    """把计划 checkpoint 的 modify 反馈回灌成 planner 输入（Task 6.5-4）。

    真实 `StepQueryPlanner.plan(question, classes, client, model_name)` 没有反馈参数
    （Step 0 实测），故反馈只能经**问题文本**进入 —— 与 chat 注入 scopeQuestion /
    priorState 同款。反馈是用户内容，先经 `neutralizeFence` 打断围栏标签，避免其
    提前闭合 `<user_content>` 被当成指令执行。
    """
    if not feedback:
        return question
    payload = neutralizeFence(json.dumps(feedback, ensure_ascii=False, sort_keys=True))
    return f"{question}\n\n{PLAN_FEEDBACK_HEADER} {payload}"


def resumeState(row: Any, checkpoint: Any, choice: dict[str, Any] | None) -> dict[str, Any]:
    """重建恢复态并补回会话级模型选择（W5）：checkpoint 载荷不带 modelId（模型不是落库状态）。

    键序与恢复前一致（rebuildState → modelId → choice），故 `state` 形状逐字不变。
    """
    state = rebuildState(row, checkpoint)
    state["modelId"] = row.model_id
    state["choice"] = choice or {}
    return state


def nextPhase(checkpoint: Any, action: str) -> str:
    """决策后回到哪个相位；动态点 reject 时走 `abortPhase`（终止到出报告）。"""
    options = checkpoint.options or {}
    if action == ACTION_REJECT and options.get(OPT_ABORT_PHASE):
        return str(options[OPT_ABORT_PHASE])
    return nextPhaseForPhase(checkpoint.phase, options)


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
