"""研究入口 turn 状态机（feat-research-entry Task 5）。

设计 §4.3 的 12 步压缩为 7 个相位：intent → esl → plan → execute → hypothesis →
verify → report。三个**固定 checkpoint**（范围确认 / 计划确认 / 假设挑选）与两个
**动态相位**（`runtime_dynamic`：ESL 冲突；`low_confidence_step`：执行步失败或空数据）
在指定相位暂停：状态落 `research_checkpoint`（`options` 带恢复所需的语义载荷），
用户决策后 `resumeTurn` 从「下一相位」续跑。

本模块只留编排（相位调度 + IO）；共享词汇、端口协议、默认适配器与无状态构件在
`research_agent_ports.py`（fix round 1 抽取），执行面（步 SQL 生成 → 只读查询 → 出图）在
`research_agent_execution.py`（Task 6.5 fix round 2 抽取）——两者都是为守住 800 行硬上限。
`PHASES` 在此再导出，保持 brief 的 `from app.services.research_agent_service import PHASES` 契约。

Step 0 核对（2026-10-04，本任务强制）：`grep -nE "class |def " app/services/chat_usage.py`
只有 `UsageMixin` 的 mixin 私有方法（依赖 `self._costFor` / `self._tokenUsage`），
**没有可注入的公开计量器**；复用会把研究链路耦合进 chat 域。故 `usageRecorder` 是本
项目自定义协议，默认实现 `LlmUsageRecorder` 包公开的 `TokenUsageService.recordUsage`
（写 chat 同表 `session_token_usage`，**自己 commit**）并按 model_name 反查 `llm_config`
单价现算成本。完整 grep 输出与偏差见 `.superpowers/sdd/2026-10-04-research-entry/task-5-report.md`。

事务约定（Task 4 契约）：`runner.runVerification` / `executeReadonlySql` 失败会让注入的
session 进入失败事务态（前者内建 rollback）。故在「执行 / 验证」相位边界显式 `commit`，
保证暂停点与 finding 不因下游 rollback 丢失。

恢复契约：checkpoint 的 `options["resumePhase"]` 是恢复点 **SSOT**（显式值优先于固定表）。
空 scope checkpoint 的 `resumePhase="esl"`；`resumeTurn(choice={"question": ...})` 可携带
改写后的问题，此时清空派生态并强制重跑 ESL（否则旧三臂会被带进新问题）。

降级可见性：无可用 LLM 客户端、假设生成失败、执行步失败、turn 失败都会发
`research.error {code, message}`（`emitEvent` 静默失败保护），不静默 `done`。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchSession
from app.services.enterprise_semantic_layer import EmptyResearchScopeError
from app.services.hypothesis_service import Hypothesis
from app.services.intent_service import IntentService
from app.services.model_config_service import ModelConfigService
from app.services.model_router_service import ModelRouterService
from app.services.ontology_service import OntologyService
from app.services.report_planner import ReportPlanner
from app.services.research_agent_execution import ExecutionDeps, runStep
from app.services.research_agent_ports import (
    ACTION_STATUS,
    CHECKPOINT_HYPOTHESIS,
    CHECKPOINT_INTENT,
    CHECKPOINT_LOW_CONFIDENCE,
    CHECKPOINT_PLANNING,
    CHECKPOINT_RUNTIME_DYNAMIC,
    DEFAULT_MODE,
    ERROR_HYPOTHESIS_FAILED,
    ERROR_TURN_FAILED,
    EVENT_CHECKPOINT,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_ESL,
    EVENT_FINDING,
    EVENT_HYPOTHESIS,
    EVENT_INTENT,
    EVENT_PLAN,
    EVENT_REPORT,
    PHASE_ESL,
    PHASES,
    PURPOSE_HYPOTHESIS,
    PURPOSE_PLAN,
    PURPOSE_REPORT,
    ROLE_CHECKPOINT,
    SIGNAL_EMPTY_SCOPE,
    SIGNAL_FIXED_HYPOTHESIS,
    SIGNAL_FIXED_PLAN,
    SIGNAL_FIXED_SCOPE,
    STATUS_AWAITING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    VERIFY_FAIL_FACTOR,
    VERIFY_OK_FACTOR,
    Emit,
    LlmUsageRecorder,
    MeteredClient,
    Pause,
    Reporter,
    UsageRecorder,
    buildOptions,
    candidateConfidence,
    clientModelName,
    dataSummary,
    drivers,
    emitEvent,
    eslClasses,
    findingData,
    nextPhase,
    nextPhaseForPhase,
    normalizePlan,
    planQuestionWithFeedback,
    rebuildState,
    recordUsageQuietly,
    requireQuestion,
    resolveClient,
    resumeTurnContent,
    rewriteState,
    selectedHypotheses,
    stepSignal,
)
from app.services.research_hypothesis_adapter import generateHypotheses
from app.services.research_session_service import ResearchSessionService
from app.services.token_usage_service import TokenUsageService

logger = logging.getLogger(__name__)

__all__ = ["PHASES", "ResearchAgentService"]


class ResearchAgentService:
    """研究 turn 状态机（无状态；构造注入协作者）。"""

    def __init__(
        self,
        *,
        esl: Any,
        sessionService: ResearchSessionService,
        planner: Any,
        runner: Any,
        chartService: Any,
        llmFactory: Callable[[Any], Any] | None = None,
        usageRecorder: UsageRecorder | None = None,
        reporter: Reporter | None = None,
        modelConfigs: Any | None = None,
        modelRouter: Any | None = None,
        ontology: Any | None = None,
        nl2sql: Any | None = None,
        tokenUsage: Any | None = None,
        autoConfirm: bool = False,
    ) -> None:
        self._esl = esl
        self._sessions = sessionService
        self._planner = planner
        self._runner = runner
        self._llmFactory = llmFactory
        self._usage = usageRecorder if usageRecorder is not None else LlmUsageRecorder()
        self._autoConfirm = autoConfirm
        self._intent = IntentService()
        self._wireRouting(modelConfigs, modelRouter, tokenUsage)
        self._wireNl2sql(ontology, nl2sql)
        # 报告装配器：默认真实 ReportPlanner（Task 6 接线）。**不注入 llmClientFactory**
        # （Task 6.5-3 / F3）：compose 的自建客户端路径绕过 MeteredClient，是未计量盲区；
        # 装配所需客户端一律由 `_stageReport` 经计量边界传入，降级时传 None（模板文案）。
        self._reporter = reporter if reporter is not None else ReportPlanner(
            sessionService=sessionService
        )
        self._exec = self._buildExec(runner, chartService, nl2sql)
        self._stages = self._buildStages()

    def _wireRouting(
        self, modelConfigs: Any | None, modelRouter: Any | None, tokenUsage: Any | None
    ) -> None:
        """模型路由端口（Task 6.5-2 / M1）：`llmFactory` 收到的必须是**已选配置**。

        三端口均可注入（测试注入确定性 fake）；默认即 chat 同源的公开实现。
        `tokenUsage` 供装配路由上下文（成本 / 轮次 / 上次模型）——chat 同口径。
        """
        self._modelConfigs = modelConfigs if modelConfigs is not None else ModelConfigService()
        self._modelRouter = modelRouter if modelRouter is not None else ModelRouterService()
        self._tokenUsage = tokenUsage if tokenUsage is not None else TokenUsageService()

    def _wireNl2sql(self, ontology: Any | None, nl2sql: Any | None) -> None:
        """步 SQL 生成端口（Task 6.5-1）：本体类按 ESL 物理表筛。

        `nl2sql` 未注入时该能力关闭（无 sql 的步仍走 STEP_MISSING_SQL，与接线前一致）。
        """
        self._ontology = ontology if ontology is not None else OntologyService()
        self._nl2sql = nl2sql

    def _buildExec(self, runner: Any, chartService: Any, nl2sql: Any | None) -> ExecutionDeps:
        """执行面依赖（Task 6.5 fix round 2 抽到 research_agent_execution）：一次绑定。"""
        return ExecutionDeps(
            runner=runner,
            chart=chartService,
            ontology=self._ontology,
            nl2sql=nl2sql,
            resolveClient=self._resolveClient,
            recordUsage=self._recordUsage,
        )

    def _buildStages(self) -> dict[str, Any]:
        """相位调度表（键 = `PHASES` 的元素，顺序由 `PHASES` 决定）。"""
        return {
            "intent": self._stageIntent,
            "esl": self._stageEsl,
            "plan": self._stagePlan,
            "execute": self._stageExecute,
            "hypothesis": self._stageHypothesis,
            "verify": self._stageVerify,
            "report": self._stageReport,
        }

    @property
    def sessionService(self) -> ResearchSessionService:
        """公开句柄：调用方（API 层 / 测试）读会话、checkpoint、报告状态用。"""
        return self._sessions

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def startTurn(
        self, session: AsyncSession, *, sessionId: uuid.UUID, question: str,
        userId: int | None, emit: Emit | None = None,
    ) -> str:
        """开一轮研究：写 user turn → 跑状态机；返回 `awaiting_user` 或 `done`。"""
        requireQuestion(question, sessionId)  # 守卫 SSOT（与 runTurn 同源）
        row = await self._loadSession(session, sessionId)
        turn = await self._sessions.appendTurn(
            session, sessionId=sessionId, role="user", content={"question": question}
        )
        logger.info("研究 turn 开始: session=%s turn=%s userId=%s", sessionId, turn.id, userId)
        return await self.runTurn(
            session, sessionId=sessionId, turnId=turn.id, question=question,
            userId=userId, mode=row.mode, emit=emit,
        )

    async def runTurn(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        question: str, userId: int | None, mode: str, emit: Emit | None = None,
    ) -> str:
        """从**已落库的 user turn** 起跑状态机；返回 `awaiting_user` 或 `done`。

        与 `startTurn` 的唯一差别是「谁写 user turn」：API 层必须先拿到 turnId 才能
        立刻回 202（Task 7），故 user turn 由调用方写入后经本方法续跑；`startTurn`
        即 `appendTurn` + 本方法的等价组合（行为不变，Task 7 抽取）。

        Task 7.5 LOW：空白问题守卫下沉到此处（API 路径 `createTurn → appendTurn →
        runTurn` 绕过 `startTurn`）；守卫在任何写/状态推进之前，空问题不推进状态机。
        """
        requireQuestion(question, sessionId)
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_RUNNING)
        state: dict[str, Any] = {
            "question": question,
            "mode": mode,
            "userId": userId,
            "esl": None,
            "plan": None,
            "stepResults": [],
            "hypotheses": [],
            "resumeStepIndex": 0,
            "choice": {},
            "llmUnavailable": False,
        }
        return await self._guardedRun(
            session, sessionId=sessionId, turnId=turnId, phase=PHASES[0], emit=emit, state=state
        )

    async def resumeTurn(
        self,
        session: AsyncSession,
        *,
        checkpointId: uuid.UUID,
        action: str,
        choice: dict[str, Any],
        emit: Emit | None = None,
    ) -> str:
        """按用户决策续跑：resolve checkpoint → 回到下一相位。

        `choice["question"]` 非空表示用户**改写了问题**（空 scope 通道）：换问题、清派生
        数据并强制重跑 ESL；恢复相位默认取 checkpoint 的 `options["resumePhase"]`。
        """
        status = ACTION_STATUS.get(action)
        if status is None:
            logger.warning("非法 checkpoint 动作: %s", action)
            raise ValueError(f"非法 checkpoint 动作 {action}，合法值: {sorted(ACTION_STATUS)}")
        checkpoint = await self._sessions.resolveCheckpoint(
            session, checkpointId=checkpointId, status=status, userChoice=choice or {}
        )
        row = await self._loadSession(session, checkpoint.session_id)
        rewritten = await self._recordResumeTurn(
            session, checkpoint, action=action, choice=choice, checkpointId=checkpointId
        )
        state = rebuildState(row, checkpoint)
        state["choice"] = choice or {}
        startPhase = nextPhase(checkpoint, action)
        if rewritten:
            state = rewriteState(state, rewritten)
            startPhase = PHASE_ESL
        elif action == "modify" and checkpoint.phase == CHECKPOINT_PLANNING:
            # Task 6.5-4（MEDIUM-8）：计划点的 modify 不是「确认旧计划继续跑」，而是带
            # 用户反馈**重跑 planner**；故回到 plan 相位并置 replan 标记（该轮不再暂停）。
            startPhase = "plan"
            state["replan"] = True
        logger.info(
            "研究 turn 恢复: session=%s checkpoint=%s action=%s startPhase=%s rewritten=%s",
            checkpoint.session_id, checkpointId, action, startPhase, bool(rewritten),
        )
        return await self._guardedRun(
            session,
            sessionId=checkpoint.session_id,
            turnId=checkpoint.turn_id,
            phase=startPhase,
            emit=emit,
            state=state,
        )

    async def _recordResumeTurn(
        self,
        session: AsyncSession,
        checkpoint: Any,
        *,
        action: str,
        choice: dict[str, Any],
        checkpointId: uuid.UUID,
    ) -> str:
        """写 user turn + 会话置 running；返回改写后的问题（空串 = 用户未改写）。"""
        rewritten = str((choice or {}).get("question") or "").strip()
        content = resumeTurnContent(
            action=action, choice=choice, checkpointId=checkpointId, rewritten=rewritten
        )
        await self._sessions.appendTurn(
            session, sessionId=checkpoint.session_id, role="user", content=content
        )
        await self._sessions.updateSessionStatus(session, checkpoint.session_id, STATUS_RUNNING)
        return rewritten

    # ------------------------------------------------------------------
    # 编排骨架
    # ------------------------------------------------------------------

    async def _guardedRun(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        phase: str,
        emit: Emit | None,
        state: dict[str, Any],
    ) -> str:
        """跑状态机；异常时发 `research.error`、把会话标 failed，再原样上抛（不吞错）。"""
        try:
            return await self._runFrom(
                session, sessionId=sessionId, turnId=turnId, phase=phase, emit=emit, state=state
            )
        except Exception as exc:
            logger.error(
                "研究 turn 失败: session=%s turn=%s phase=%s err=%s",
                sessionId,
                turnId,
                phase,
                exc,
                exc_info=True,
            )
            await emitEvent(
                emit,
                EVENT_ERROR,
                {"code": ERROR_TURN_FAILED, "message": str(exc), "phase": phase},
            )
            await self._markFailed(session, sessionId)
            raise

    async def _runFrom(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        phase: str,
        emit: Emit | None,
        state: dict[str, Any],
    ) -> str:
        """从 `phase` 起顺序跑各相位；任一相位返回暂停信号即落 checkpoint 并返回。"""
        for current in PHASES[PHASES.index(phase):]:
            pause = await self._stages[current](
                session, sessionId=sessionId, turnId=turnId, emit=emit, state=state
            )
            if pause is not None:
                await self._pauseForUser(
                    session, sessionId=sessionId, turnId=turnId, pause=pause, emit=emit
                )
                return STATUS_AWAITING
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_DONE)
        await emitEvent(
            emit,
            EVENT_DONE,
            {
                "sessionId": str(sessionId),
                "reportId": state.get("reportId"),
                "degraded": bool(state.get("llmUnavailable")),
            },
        )
        return STATUS_DONE

    async def _pauseForUser(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        pause: Pause,
        emit: Emit | None,
    ) -> None:
        """落 checkpoint + checkpoint_awaiting turn + 会话置 awaiting_user。"""
        phase, options, prompt = pause
        checkpoint = await self._sessions.openCheckpoint(
            session,
            sessionId=sessionId,
            turnId=turnId,
            phase=phase,
            options=options,
            prompt=prompt,
        )
        await self._sessions.appendTurn(
            session,
            sessionId=sessionId,
            role=ROLE_CHECKPOINT,
            content={
                "checkpointId": str(checkpoint.id),
                "phase": phase,
                # 仅作展示/调试用途：恢复点 SSOT 是 checkpoint.options["resumePhase"]
                "nextPhase": nextPhaseForPhase(phase, options),
                "prompt": prompt,
                "options": options,
            },
        )
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_AWAITING)
        await emitEvent(
            emit,
            EVENT_CHECKPOINT,
            {
                "checkpointId": str(checkpoint.id),
                "phase": phase,
                "prompt": prompt,
                "options": options,
            },
        )

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
                    resumePhase="plan",
                    conflicts=arms["conflicts"],
                ),
                "检测到语义歧义，如何处理？",
            )
        return (
            CHECKPOINT_INTENT,
            buildOptions(signal=SIGNAL_FIXED_SCOPE, arms=arms, resumePhase="plan"),
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
        plan = normalizePlan(getattr(result, "plan", result))
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
                signal=SIGNAL_FIXED_PLAN, plan=plan, arms=state.get("esl"), resumePhase="execute"
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
        results: list[dict[str, Any]] = list(state.get("stepResults") or [])
        startIndex = int(state.get("resumeStepIndex") or 0)
        # Task 4 契约：执行失败会 rollback 注入的 session，先固化本 turn 已写入的状态行
        await session.commit()
        for step in steps:
            if int(step.get("index", 0)) < startIndex:
                continue
            result = await runStep(
                session, step, sessionId=sessionId, state=state, emit=emit, deps=self._exec
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
                    stepResults=results,
                    stepIndex=result["index"],
                    error=result["error"] or "该步骤无数据返回",
                    resumePhase="execute",
                    abortPhase="report",
                    nextStepIndex=result["index"] + 1,
                ),
                "步骤失败，跳过还是终止？",
            )
        state["stepResults"] = results
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
                arms=state.get("esl"),
                resumePhase="verify",
            ),
            "验证哪些假设？",
        )

    async def _stageVerify(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[9] 逐条验证假设并落 finding（confidence = 候选分 × 0.9 / 0.3）。"""
        findings: list[dict[str, Any]] = []
        for candidate in selectedHypotheses(state):
            await session.commit()  # Task 4 契约：runVerification 失败会 rollback 本 session
            outcome = await self._runner.runVerification(session, Hypothesis(**candidate))
            verified = outcome.get("error") is None
            confidence = round(
                candidateConfidence(state, candidate)
                * (VERIFY_OK_FACTOR if verified else VERIFY_FAIL_FACTOR),
                6,
            )
            row = await self._sessions.saveFinding(
                session,
                sessionId=sessionId,
                turnId=turnId,
                claimText=candidate["statement"],
                supportingSql=candidate.get("verificationSql"),
                supportingData=self._findingData(outcome),
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

    @staticmethod
    def _findingData(outcome: dict[str, Any]) -> dict[str, Any]:
        """finding.supporting_data 契约（实现见 ports.findingData，Task 6 报告读侧）。"""
        return findingData(outcome)

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
                "degraded": bool(state.get("llmUnavailable")),
            },
        )
        return None

    async def _generateCandidates(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        state: dict[str, Any],
        emit: Emit | None,
    ) -> list[dict[str, Any]]:
        """假设生成（LLM）；无客户端或解析失败都降级为空候选，用量照记、失败显式可见。"""
        client = await self._llmClient(session, state=state, emit=emit, sessionId=sessionId)
        if client is None:
            return []
        metered = MeteredClient(client)
        candidates: list[dict[str, Any]] = []
        try:
            hypotheses = await generateHypotheses(
                metered, state["question"], dataSummary(state), drivers(state)
            )
            candidates = [asdict(h) for h in hypotheses]
        except Exception:  # noqa: BLE001 —— 假设生成失败降级为空候选（研究链路不因它中断）
            logger.error("假设生成失败，降级为空候选: session=%s", sessionId, exc_info=True)
            await emitEvent(
                emit,
                EVENT_ERROR,
                {"code": ERROR_HYPOTHESIS_FAILED, "message": "假设生成失败，本轮无候选假设"},
            )
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_HYPOTHESIS,
            promptTokens=metered.promptTokens,
            completionTokens=metered.completionTokens,
            modelName=metered.modelName,
            cachedTokens=metered.cachedTokens,
        )
        return candidates

    # ------------------------------------------------------------------
    # LLM 客户端 / 计量 / 兜底
    # ------------------------------------------------------------------

    async def _llmClient(
        self,
        session: AsyncSession,
        *,
        state: dict[str, Any],
        emit: Emit | None,
        sessionId: uuid.UUID,
    ) -> Any:
        """取 LLM 客户端（按 chat 同口径路由选模型）；取不到时显式留痕（见 ports.resolveClient）。"""
        client, _config = await self._resolveClient(
            session, state=state, emit=emit, sessionId=sessionId
        )
        return client

    async def _resolveClient(
        self,
        session: AsyncSession,
        *,
        state: dict[str, Any],
        emit: Emit | None,
        sessionId: uuid.UUID,
    ) -> tuple[Any, Any]:
        """取（客户端, 模型配置）二元组：步 SQL 生成需要配置透传给 NL2SQL。"""
        return await resolveClient(
            self._llmFactory,
            self._modelConfigs,
            self._modelRouter,
            session,
            state=state,
            emit=emit,
            sessionId=sessionId,
            tokenUsage=self._tokenUsage,
        )

    async def _recordUsage(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        purpose: str,
        promptTokens: int,
        completionTokens: int,
        modelName: str | None,
        cachedTokens: int | None = None,
    ) -> None:
        """记一次 LLM 用量（实现见 ports.recordUsageQuietly：零消耗跳过、失败只留痕）。"""
        await recordUsageQuietly(
            self._usage,
            session,
            sessionId=sessionId,
            purpose=purpose,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            modelName=modelName,
            cachedTokens=cachedTokens,
        )

    async def _markFailed(self, session: AsyncSession, sessionId: uuid.UUID) -> None:
        """把会话标 failed（best-effort：此处已在异常路径，不再抛二次异常）。"""
        try:
            await self._sessions.updateSessionStatus(session, sessionId, STATUS_FAILED)
        except Exception:  # noqa: BLE001 —— 失败态落库失败不能盖住原始异常
            logger.error("标记会话失败态失败: session=%s", sessionId, exc_info=True)

    async def _loadSession(self, session: AsyncSession, sessionId: uuid.UUID) -> ResearchSession:
        row = await session.get(ResearchSession, sessionId)
        if row is None:
            logger.warning("研究会话不存在: id=%s", sessionId)
            raise ValueError(f"研究会话不存在: {sessionId}")
        return row
