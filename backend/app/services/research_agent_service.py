"""研究入口 turn 状态机（feat-research-entry Task 5）。

设计 §4.3 的 12 步压缩为 7 个相位：intent → esl → plan → execute → hypothesis →
verify → report。三个**固定 checkpoint**（范围确认 / 计划确认 / 假设挑选）与两个
**动态相位**（`runtime_dynamic`：ESL 冲突；`low_confidence_step`：执行步失败或空数据）
在指定相位暂停：状态落 `research_checkpoint`（`options` 带恢复所需的语义载荷），
用户决策后 `resumeTurn` 从「下一相位」续跑。

Step 0 核对（2026-10-04，本任务强制）：`grep -nE "class |def " app/services/chat_usage.py`
只有 `UsageMixin` 的 mixin 私有方法（`_recordUsage` / `_recordDirectUsage`，依赖
`self._costFor` / `self._tokenUsage`），**没有可注入的独立计量器**；直接复用会把研究
链路耦合进 chat 域。故 `usageRecorder` 是本模块定义的**协议**，默认实现
`LlmUsageRecorder` 包公开的 `TokenUsageService.recordUsage(...)`（写 chat 同表
`session_token_usage`，**自己 commit**）并按 model_name 反查 `llm_config` 单价现算
成本（与 chat `_costFor` 同公式）。完整 grep 输出与偏差见
`.superpowers/sdd/2026-10-04-research-entry/task-5-report.md`。

事务约定（Task 4 契约）：`runner.runVerification` / `executeReadonlySql` 失败会让注入的
session 进入失败事务态，前者内建 rollback。故本状态机在「执行 / 验证」相位边界显式
`commit`，保证暂停点与 finding 不因下游 rollback 丢失。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.domain.research_models import ResearchFinding, ResearchSession
from app.services.enterprise_semantic_layer import EmptyResearchScopeError
from app.services.hypothesis_service import Hypothesis
from app.services.intent_service import IntentService
from app.services.research_hypothesis_adapter import generateHypotheses
from app.services.research_session_service import ResearchSessionService
from app.services.token_usage_service import TokenUsageService

logger = logging.getLogger(__name__)

PHASES = ("intent", "esl", "plan", "execute", "hypothesis", "verify", "report")
"""状态机相位（顺序即执行顺序）。"""

# --- checkpoint 相位（落 research_checkpoint.phase，长度上限 30）-------------
CHECKPOINT_INTENT = "intent"  # 固定 #1 三臂范围确认（由 _stageEsl 开启）
CHECKPOINT_PLANNING = "planning"  # 固定 #2 计划确认（由 _stagePlan 开启）
CHECKPOINT_HYPOTHESIS = "hypothesis"  # 固定 #3 假设挑选（由 _stageHypothesis 开启）
CHECKPOINT_RUNTIME_DYNAMIC = "runtime_dynamic"  # 动态：ESL 冲突，决策后直进 plan
CHECKPOINT_LOW_CONFIDENCE = "low_confidence_step"  # 动态：执行步失败/空数据

# 固定 checkpoint 的恢复点；动态 checkpoint 从 options["resumePhase"] 取
NEXT_PHASE_BY_CHECKPOINT = {
    CHECKPOINT_INTENT: "plan",
    CHECKPOINT_PLANNING: "execute",
    CHECKPOINT_HYPOTHESIS: "verify",
}

# --- 动态信号名（设计 §4.8，落 options["signal"]）----------------------------
SIGNAL_FIXED_SCOPE = "fixed_scope"
SIGNAL_FIXED_PLAN = "fixed_plan"
SIGNAL_FIXED_HYPOTHESIS = "fixed_hypothesis"
SIGNAL_EMPTY_SCOPE = "empty_scope"
SIGNAL_LOW_CONFIDENCE_STEP = "low_confidence_step"
SIGNAL_SQL_VALIDATION_FAILED = "sql_validation_failed"

# --- options 键名 ------------------------------------------------------------
OPT_SIGNAL = "signal"
OPT_ARMS = "arms"
OPT_PLAN = "plan"
OPT_CANDIDATES = "candidates"
OPT_STEP_RESULTS = "stepResults"
OPT_RESUME_PHASE = "resumePhase"
OPT_ABORT_PHASE = "abortPhase"
OPT_NEXT_STEP = "nextStepIndex"
OPT_STEP_INDEX = "stepIndex"
OPT_ERROR = "error"
OPT_CONFLICTS = "conflicts"

# --- 会话/轮次状态（Task 3 白名单：running|awaiting_user|done|failed|aborted）---
STATUS_RUNNING = "running"
STATUS_AWAITING = "awaiting_user"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
ROLE_CHECKPOINT = "checkpoint_awaiting"

# 用户动作 → checkpoint 状态（Task 3 review 裁定，不允许其它写法）。不落 "consumed"：
# resolveCheckpoint 后 getPendingCheckpoint 返 None 即 consumed 语义。
ACTION_STATUS = {"confirm": "confirmed", "modify": "modified", "reject": "rejected"}
ACTION_REJECT = "reject"

# --- 计量 purpose / 置信度 / 摘要上限 ----------------------------------------
PURPOSE_PLAN = "research_plan"
PURPOSE_HYPOTHESIS = "research_hypothesis"
VERIFY_OK_FACTOR = 0.9  # 验证成功：候选分 × 0.9
VERIFY_FAIL_FACTOR = 0.3  # 验证失败：候选分 × 0.3（失败本身是合法结论，不是 0）
DEFAULT_CANDIDATE_CONFIDENCE = 0.5  # 假设 driver 未命中 ESL 候选时的基线分
MAX_VERIFY_HYPOTHESES = 3
DRIVER_HINT_LIMIT = 30
COLUMN_SUMMARY_LIMIT = 12
STEP_MISSING_SQL = "计划步未携带 SQL（逐步 NL2SQL 生成不在本任务注入面内）"

# --- SSE 事件名（设计 §4.5）/ 默认 mode --------------------------------------
EVENT_INTENT = "research.intent"
EVENT_ESL = "research.esl"
EVENT_CHECKPOINT = "research.checkpoint"
EVENT_PLAN = "research.plan"
EVENT_STEP_START = "research.step.start"
EVENT_STEP_DONE = "research.step.done"
EVENT_HYPOTHESIS = "research.hypothesis"
EVENT_FINDING = "research.finding"
EVENT_REPORT = "research.report"
EVENT_DONE = "research.done"
DEFAULT_MODE = "research"

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
Pause = tuple[str, dict[str, Any], str]  # (checkpoint 相位, options, 提示文案)


class UsageRecorder(Protocol):
    """LLM 用量记账契约（落库口径由实现决定，默认写 chat 同表 `session_token_usage`）。"""

    async def recordUsage(
        self,
        session: AsyncSession,
        *,
        sessionId: str,
        purpose: str,
        promptTokens: int,
        completionTokens: int,
        modelName: str | None,
    ) -> None: ...


class Reporter(Protocol):
    """ReportPlanner（Task 6）契约：`compose(...) -> (payload, rendered_md)`。"""

    async def compose(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        mode: str,
        llmClient: Any = None,
    ) -> tuple[dict[str, Any], str]: ...


class LlmUsageRecorder:
    """默认计量实现：写 `session_token_usage`（与 chat 同表同口径）。

    chat 侧 `_recordUsage` 的单价来自已选 `LlmConfig`；研究链路没有 modelConfig 透传，
    故按 `model_name` 反查 `llm_config` 现算成本（同一公式
    `pt/1000*in + ct/1000*out`）。查不到配置 ⇒ modelConfigId=None、成本 0 并 warning
    （显式留痕，不静默丢账）。零消耗直接跳过，不写空行。
    """

    def __init__(self, tokenUsage: TokenUsageService | None = None) -> None:
        self._tokenUsage = tokenUsage or TokenUsageService()

    async def recordUsage(
        self,
        session: AsyncSession,
        *,
        sessionId: str,
        purpose: str,
        promptTokens: int,
        completionTokens: int,
        modelName: str | None,
    ) -> None:
        if promptTokens + completionTokens <= 0:
            return
        config = await self._findConfig(session, modelName)
        if config is None and modelName:
            logger.warning("计量未匹配到 llm_config，成本按 0 记: model=%s", modelName)
        cost = self._costFor(config, promptTokens, completionTokens)
        await self._tokenUsage.recordUsage(
            session,
            sessionId=sessionId,
            modelConfigId=getattr(config, "id", None),
            modelName=modelName,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            cost=cost,
            purpose=purpose,
        )

    @staticmethod
    async def _findConfig(session: AsyncSession, modelName: str | None) -> LlmConfig | None:
        if not modelName:
            return None
        return await session.scalar(
            select(LlmConfig).where(LlmConfig.model_name == modelName).limit(1)
        )

    @staticmethod
    def _costFor(config: LlmConfig | None, promptTokens: int, completionTokens: int) -> Decimal:
        if config is None:
            return Decimal("0")
        return (
            Decimal(promptTokens) * Decimal(str(config.cost_per_1k_input))
            + Decimal(completionTokens) * Decimal(str(config.cost_per_1k_output))
        ) / Decimal(1000)


class _MeteredClient:
    """LLM 客户端包装：透传 `complete`，捕获最近一次调用的 token 与模型名。

    适配层 `generateHypotheses` 只回传假设列表（token 在响应里被丢掉），故计量必须在
    客户端边界捕获——否则「假设生成的 LLM 调用」成为计量盲区（核心约束 #3）。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.promptTokens = 0
        self.completionTokens = 0
        self.modelName: str | None = None

    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        response = await self._inner.complete(messages, **kwargs)
        self.promptTokens = int(getattr(response, "promptTokens", 0) or 0)
        self.completionTokens = int(getattr(response, "completionTokens", 0) or 0)
        self.modelName = getattr(response, "modelName", None) or self.modelName
        return response


class _DefaultReporter:
    """ReportPlanner 占位实现（Task 6 落地前）：确定性拼装，零 LLM。

    签名与 Task 6 `ReportPlanner.compose` 一致，Task 6 完成后由注入的 reporter 替换
    （plan Task 6 Step 5）。只读 finding 行，不做 LLM 文字生成。
    """

    async def compose(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        mode: str,
        llmClient: Any = None,
    ) -> tuple[dict[str, Any], str]:
        findings = list(
            await session.scalars(
                select(ResearchFinding)
                .where(ResearchFinding.session_id == sessionId)
                .order_by(ResearchFinding.created_at.asc())
            )
        )
        payload = {
            "sessionId": str(sessionId),
            "turnId": str(turnId),
            "mode": mode,
            "sections": [
                {
                    "id": "findings",
                    "kind": "table",
                    "title": "验证结论",
                    "blocks": [
                        {
                            "type": "text",
                            "content": f"{row.claim_text}（置信度 {row.confidence}）",
                            "sourceRefs": [{"kind": "finding", "refId": str(row.id)}],
                        }
                        for row in findings
                    ],
                }
            ],
        }
        lines = [f"# 研究报告（{mode}）", ""]
        lines += [f"- {row.claim_text}（置信度 {row.confidence}）" for row in findings]
        if not findings:
            lines.append("- （本轮无已验证结论）")
        return payload, "\n".join(lines)


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
        autoConfirm: bool = False,
    ) -> None:
        self._esl = esl
        self._sessions = sessionService
        self._planner = planner
        self._runner = runner
        self._chart = chartService
        self._llmFactory = llmFactory
        self._usage = usageRecorder if usageRecorder is not None else LlmUsageRecorder()
        self._reporter = reporter if reporter is not None else _DefaultReporter()
        self._autoConfirm = autoConfirm
        self._intent = IntentService()
        self._stages: dict[str, Any] = {
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
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        question: str,
        userId: int | None,
        emit: Emit | None = None,
    ) -> str:
        """开一轮研究：写 user turn → 跑状态机；返回 `awaiting_user` 或 `done`。"""
        if not question or not question.strip():
            logger.warning("研究 turn 问题为空: session=%s", sessionId)
            raise ValueError("研究问题不能为空")
        row = await self._loadSession(session, sessionId)
        turn = await self._sessions.appendTurn(
            session, sessionId=sessionId, role="user", content={"question": question}
        )
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_RUNNING)
        state: dict[str, Any] = {
            "question": question,
            "mode": row.mode,
            "userId": userId,
            "esl": None,
            "plan": None,
            "stepResults": [],
            "hypotheses": [],
            "resumeStepIndex": 0,
            "choice": {},
        }
        logger.info("研究 turn 开始: session=%s turn=%s userId=%s", sessionId, turn.id, userId)
        return await self._guardedRun(
            session, sessionId=sessionId, turnId=turn.id, phase=PHASES[0], emit=emit, state=state
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
        """按用户决策续跑：resolve checkpoint → 回到下一相位。"""
        status = ACTION_STATUS.get(action)
        if status is None:
            logger.warning("非法 checkpoint 动作: %s", action)
            raise ValueError(f"非法 checkpoint 动作 {action}，合法值: {sorted(ACTION_STATUS)}")
        checkpoint = await self._sessions.resolveCheckpoint(
            session, checkpointId=checkpointId, status=status, userChoice=choice or {}
        )
        row = await self._loadSession(session, checkpoint.session_id)
        await self._sessions.appendTurn(
            session,
            sessionId=checkpoint.session_id,
            role="user",
            content={
                "action": action,
                "choice": choice or {},
                "checkpointId": str(checkpointId),
            },
        )
        await self._sessions.updateSessionStatus(session, checkpoint.session_id, STATUS_RUNNING)
        nextPhase = self._nextPhase(checkpoint, action)
        state = self._rebuildState(row, checkpoint)
        state["choice"] = choice or {}
        logger.info(
            "研究 turn 恢复: session=%s checkpoint=%s action=%s nextPhase=%s",
            checkpoint.session_id,
            checkpointId,
            action,
            nextPhase,
        )
        return await self._guardedRun(
            session,
            sessionId=checkpoint.session_id,
            turnId=checkpoint.turn_id,
            phase=nextPhase,
            emit=emit,
            state=state,
        )

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
        """跑状态机；异常时把会话标 failed 后原样上抛（不吞错）。"""
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
        await self._emit(
            emit,
            EVENT_DONE,
            {"sessionId": str(sessionId), "reportId": state.get("reportId")},
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
                "nextPhase": self._nextPhaseForPhase(phase, options),
                "prompt": prompt,
                "options": options,
            },
        )
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_AWAITING)
        await self._emit(
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
        await self._emit(emit, EVENT_INTENT, dict(state["intent"]))
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
                self._options(
                    state,
                    signal=SIGNAL_EMPTY_SCOPE,
                    arms=emptyArms,
                    resumePhase="esl",
                ),
                "三臂检索全空：请改写问题后继续",
            )
        arms = asdict(extraction)
        state["esl"] = arms
        await self._emit(emit, EVENT_ESL, arms)
        if self._autoConfirm:
            return None
        if extraction.conflicts:
            # 动态点先于固定 #1：冲突未决时范围确认没有意义（且 brief 的契约测试要求
            # 第一个 pending checkpoint 就是 runtime_dynamic）。
            kinds = sorted({c.kind for c in extraction.conflicts})
            return (
                CHECKPOINT_RUNTIME_DYNAMIC,
                self._options(
                    state,
                    signal=kinds[0],
                    arms=arms,
                    resumePhase="plan",
                    conflicts=arms["conflicts"],
                ),
                "检测到语义歧义，如何处理？",
            )
        return (
            CHECKPOINT_INTENT,
            self._options(state, signal=SIGNAL_FIXED_SCOPE, arms=arms, resumePhase="plan"),
            "三臂是否齐全？",
        )

    async def _stagePlan(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[4][5] 多步计划（LLM，计量）→ 固定 #2 计划确认。"""
        client = self._llmClient()
        result = await self._planner.plan(
            state["question"], self._eslClasses(state), client, self._clientModelName(client)
        )
        plan = self._normalizePlan(getattr(result, "plan", result))
        state["plan"] = plan
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_PLAN,
            promptTokens=int(getattr(result, "prompt_tokens", 0) or 0),
            completionTokens=int(getattr(result, "completion_tokens", 0) or 0),
            modelName=self._clientModelName(client),
        )
        await self._emit(emit, EVENT_PLAN, {"steps": plan["steps"]})
        if self._autoConfirm:
            return None
        return (
            CHECKPOINT_PLANNING,
            self._options(
                state, signal=SIGNAL_FIXED_PLAN, plan=plan, arms=state.get("esl"),
                resumePhase="execute",
            ),
            "计划是否确认？",
        )

    async def _stageExecute(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[6] 逐步只读执行 + 出图；步失败/空数据 → 动态 low_confidence_step 点。"""
        steps = [s for s in (state.get("plan") or {}).get("steps", []) if not s.get("aggregation_only")]
        results: list[dict[str, Any]] = list(state.get("stepResults") or [])
        startIndex = int(state.get("resumeStepIndex") or 0)
        # Task 4 契约：执行失败会 rollback 注入的 session，先固化本 turn 已写入的状态行
        await session.commit()
        for step in steps:
            if int(step.get("index", 0)) < startIndex:
                continue
            result = await self._runStep(session, step, sessionId=sessionId, state=state, emit=emit)
            results.append(result)
            state["stepResults"] = results
            if not result["error"] and result["rowCount"] > 0:
                continue
            if self._autoConfirm:
                continue
            return (
                CHECKPOINT_LOW_CONFIDENCE,
                self._options(
                    state,
                    signal=self._stepSignal(result["error"]),
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
        candidates = await self._generateCandidates(session, sessionId=sessionId, state=state)
        state["hypotheses"] = candidates
        await self._emit(emit, EVENT_HYPOTHESIS, {"candidates": candidates})
        if self._autoConfirm:
            return None
        return (
            CHECKPOINT_HYPOTHESIS,
            self._options(
                state, signal=SIGNAL_FIXED_HYPOTHESIS, candidates=candidates,
                arms=state.get("esl"), resumePhase="verify",
            ),
            "验证哪些假设？",
        )

    async def _stageVerify(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[9] 逐条验证假设并落 finding（confidence = 候选分 × 0.9 / 0.3）。"""
        findings: list[dict[str, Any]] = []
        for candidate in self._selectedHypotheses(state):
            await session.commit()  # Task 4 契约：runVerification 失败会 rollback 本 session
            outcome = await self._runner.runVerification(session, Hypothesis(**candidate))
            verified = outcome.get("error") is None
            confidence = round(
                self._candidateConfidence(state, candidate)
                * (VERIFY_OK_FACTOR if verified else VERIFY_FAIL_FACTOR),
                6,
            )
            row = await self._sessions.saveFinding(
                session,
                sessionId=sessionId,
                turnId=turnId,
                claimText=candidate["statement"],
                supportingSql=candidate.get("verificationSql"),
                supportingData={
                    "rowCount": len(outcome.get("rows") or []),
                    "error": outcome.get("error"),
                },
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
            await self._emit(emit, EVENT_FINDING, findings[-1])
        state["findings"] = findings
        return None

    async def _stageReport(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        emit: Emit | None, state: dict[str, Any],
    ) -> Pause | None:
        """[10][11][12] 报告装配 + 归档（reporter 由 Task 6 实现，可注入 fake）。"""
        payload, renderedMd = await self._reporter.compose(
            session,
            sessionId=sessionId,
            turnId=turnId,
            mode=state.get("mode") or DEFAULT_MODE,
            llmClient=self._llmClient(),
        )
        report = await self._sessions.publishReport(
            session, sessionId=sessionId, payload=payload, renderedMd=renderedMd
        )
        state["reportId"] = str(report.id)
        state["reportVersion"] = report.version
        await self._emit(
            emit,
            EVENT_REPORT,
            {"reportId": str(report.id), "version": report.version},
        )
        return None

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _runStep(
        self,
        session: AsyncSession,
        step: dict[str, Any],
        *,
        sessionId: uuid.UUID,
        state: dict[str, Any],
        emit: Emit | None,
    ) -> dict[str, Any]:
        """执行单个计划步：只读查询 + 出图；失败面全部收敛为 result["error"]。"""
        index = int(step.get("index", 0))
        await self._emit(
            emit, EVENT_STEP_START, {"index": index, "description": step.get("description")}
        )
        sql = step.get("sql")
        if not sql:
            logger.warning("执行步缺 SQL，按无数据跳过: session=%s step=%s", sessionId, index)
            return self._stepResult(step, rows=[], error=STEP_MISSING_SQL)
        try:
            rows = await self._runner.executeReadonlySql(session, sql)
        except ValueError as exc:
            # SQL Guard 拒绝（Task 4 裁定：ValueError 不属 DomainError 面，必须显式 catch）
            await self._rollbackAfterStepFailure(session)
            return self._stepResult(step, rows=[], error=f"{SIGNAL_SQL_VALIDATION_FAILED}: {exc}")
        except TimeoutError as exc:
            await self._rollbackAfterStepFailure(session)
            return self._stepResult(step, rows=[], error=f"执行超时: {exc}")
        except Exception as exc:  # noqa: BLE001 —— 驱动层异常（表/权限/语法）是合法「步失败」结论
            await self._rollbackAfterStepFailure(session)
            logger.warning("执行步失败: session=%s step=%s err=%s", sessionId, index, exc)
            return self._stepResult(step, rows=[], error=str(exc))
        return await self._chartStep(session, step, rows, state=state, emit=emit)

    async def _chartStep(
        self,
        session: AsyncSession,
        step: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        state: dict[str, Any],
        emit: Emit | None,
    ) -> dict[str, Any]:
        """出图（ChartService 契约：绝不抛错）并落 result。"""
        build = await self._chart.buildChart(
            session=session,
            plan=None,
            columns=list(rows[0].keys()) if rows else [],
            data=rows,
            question=state["question"],
            llmClient=self._llmClient(),
            modelConfig=None,
        )
        result = self._stepResult(
            step,
            rows=rows,
            error=None,
            chartType=getattr(getattr(build, "chartType", None), "value", None),
            chartOption=getattr(build, "option", None),
        )
        await self._emit(
            emit,
            EVENT_STEP_DONE,
            {"index": result["index"], "rowCount": result["rowCount"], "summary": result["summary"]},
        )
        return result

    @staticmethod
    def _stepResult(
        step: dict[str, Any],
        *,
        rows: list[dict[str, Any]],
        error: str | None,
        chartType: str | None = None,
        chartOption: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """步结果（落 state / checkpoint options）：只留摘要与列名，不留原始行。"""
        columns = list(rows[0].keys()) if rows else []
        if error:
            summary = f"步骤失败：{error}"
        elif not rows:
            summary = "无数据返回"
        else:
            summary = f"返回 {len(rows)} 行，列：{'、'.join(columns[:COLUMN_SUMMARY_LIMIT])}"
        return {
            "index": int(step.get("index", 0)),
            "description": str(step.get("description") or ""),
            "subQuestion": str(step.get("sub_question") or ""),
            "sql": step.get("sql"),
            "rowCount": len(rows),
            "columns": columns,
            "summary": summary,
            "error": error,
            "chartType": chartType,
            "chartOption": chartOption,
        }

    async def _generateCandidates(
        self, session: AsyncSession, *, sessionId: uuid.UUID, state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """假设生成（LLM）；无客户端或解析失败都降级为空候选，用量照记。"""
        client = self._llmClient()
        if client is None:
            logger.warning("无可用 LLM 客户端，跳过假设生成: session=%s", sessionId)
            return []
        metered = _MeteredClient(client)
        candidates: list[dict[str, Any]] = []
        try:
            hypotheses = await generateHypotheses(
                metered, state["question"], self._dataSummary(state), self._drivers(state)
            )
            candidates = [asdict(h) for h in hypotheses]
        except Exception:  # noqa: BLE001 —— 假设生成失败降级为空候选（研究链路不因它中断）
            logger.error("假设生成失败，降级为空候选: session=%s", sessionId, exc_info=True)
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_HYPOTHESIS,
            promptTokens=metered.promptTokens,
            completionTokens=metered.completionTokens,
            modelName=metered.modelName,
        )
        return candidates

    def _selectedHypotheses(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """按用户在固定 #3 的选择（choice["selectedIndexes"]）筛假设；缺省全选。"""
        candidates = list(state.get("hypotheses") or [])
        indexes = (state.get("choice") or {}).get("selectedIndexes")
        if not isinstance(indexes, list):
            return candidates[:MAX_VERIFY_HYPOTHESES]
        picked: list[dict[str, Any]] = []
        for raw in indexes:
            if isinstance(raw, int) and 0 <= raw < len(candidates):
                picked.append(candidates[raw])
            else:
                logger.warning("假设选择下标越界，忽略: %s", raw)
        return picked[:MAX_VERIFY_HYPOTHESES]

    def _candidateConfidence(self, state: dict[str, Any], candidate: dict[str, Any]) -> float:
        """候选分：假设 driver 命中 ESL 指标/本体时的置信度；未命中取基线。"""
        driver = str(candidate.get("driver") or "").strip().lower()
        if driver:
            for name, score in self._confidenceIndex(state):
                if driver in name.lower():
                    return score
        return DEFAULT_CANDIDATE_CONFIDENCE

    @staticmethod
    def _confidenceIndex(state: dict[str, Any]) -> list[tuple[str, float]]:
        esl = state.get("esl") or {}
        pairs: list[tuple[Any, Any]] = []
        for metric in esl.get("metrics") or []:
            pairs += [(metric.get("displayName"), metric.get("confidence"))]
            pairs += [(metric.get("kpiCode"), metric.get("confidence"))]
        for obj in esl.get("businessObjects") or []:
            pairs += [(obj.get("className"), obj.get("confidence"))]
            pairs += [(obj.get("matchedAlias"), obj.get("confidence"))]
        return [(str(name), float(score)) for name, score in pairs if name and score is not None]

    @staticmethod
    def _drivers(state: dict[str, Any]) -> list[str]:
        """driver 提示：ESL 命中的指标 / 本体名（真实列名提示需 OntologyService，见报告）。"""
        esl = state.get("esl") or {}
        names: list[str] = []
        for metric in esl.get("metrics") or []:
            names += [metric.get("kpiCode"), metric.get("displayName")]
        for obj in esl.get("businessObjects") or []:
            names.append(obj.get("className"))
        seen: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.append(str(name))
        return seen[:DRIVER_HINT_LIMIT]

    @staticmethod
    def _dataSummary(state: dict[str, Any]) -> str:
        """喂给假设生成的执行摘要（只用步摘要，不塞原始行）。"""
        lines = [
            f"步骤 {int(r.get('index', 0)) + 1} {r.get('description') or ''}：{r.get('summary') or ''}"
            for r in state.get("stepResults") or []
        ]
        return "\n".join(lines) or "（本轮未取到数据）"

    @staticmethod
    def _eslClasses(state: dict[str, Any]) -> list[str]:
        """喂给计划器的本体类提示：ESL 命中 BO 的物理表名。"""
        esl = state.get("esl") or {}
        return [str(o["sourceTable"]) for o in esl.get("businessObjects") or [] if o.get("sourceTable")]

    @staticmethod
    def _normalizePlan(plan: Any) -> dict[str, Any]:
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

    def _options(
        self,
        state: dict[str, Any],
        *,
        signal: str,
        resumePhase: str,
        **payload: Any,
    ) -> dict[str, Any]:
        """checkpoint options：信号 + 恢复相位 + 语义载荷（前端卡片直接渲染）。"""
        return {
            OPT_SIGNAL: signal,
            OPT_RESUME_PHASE: resumePhase,
            **{key: value for key, value in payload.items() if value is not None},
        }

    def _nextPhaseForPhase(self, phase: str, options: dict[str, Any]) -> str:
        """checkpoint 相位 → 恢复相位（固定点查表；动态点读 options）。"""
        return (
            NEXT_PHASE_BY_CHECKPOINT.get(phase)
            or str(options.get(OPT_RESUME_PHASE) or PHASES[-1])
        )

    def _nextPhase(self, checkpoint: Any, action: str) -> str:
        """决策后回到哪个相位；动态点 reject 时走 `abortPhase`（终止到出报告）。"""
        options = checkpoint.options or {}
        if action == ACTION_REJECT and options.get(OPT_ABORT_PHASE):
            return str(options[OPT_ABORT_PHASE])
        return self._nextPhaseForPhase(checkpoint.phase, options)

    def _rebuildState(self, row: ResearchSession, checkpoint: Any) -> dict[str, Any]:
        """恢复态：从会话种子 + checkpoint.options 的语义载荷重建（不重跑 LLM 段）。"""
        options = checkpoint.options or {}
        return {
            "question": row.input_seed or "",
            "mode": row.mode,
            "userId": row.created_by,
            "esl": options.get(OPT_ARMS),
            "plan": options.get(OPT_PLAN),
            "stepResults": list(options.get(OPT_STEP_RESULTS) or []),
            "hypotheses": list(options.get(OPT_CANDIDATES) or []),
            "resumeStepIndex": int(options.get(OPT_NEXT_STEP) or 0),
            "choice": {},
        }

    @staticmethod
    def _stepSignal(error: str | None) -> str:
        """步失败信号名：SQL Guard 拒绝单列，其余归 low_confidence_step（设计 §4.8）。"""
        if error and error.startswith(SIGNAL_SQL_VALIDATION_FAILED):
            return SIGNAL_SQL_VALIDATION_FAILED
        return SIGNAL_LOW_CONFIDENCE_STEP

    def _llmClient(self) -> Any:
        """按注入的 factory 取客户端；未注入 / 取不到都返回 None（LLM 段降级跳过）。

        `llmFactory` 的模型配置选择（ModelRouterService / dto.modelId）不在 Task 5 注入面
        内，由接线任务补齐——此处 `factory(None)` 与 `createClient(None)` 契约一致
        （无配置走环境变量 key，无 key 返回 None）。
        """
        if self._llmFactory is None:
            return None
        try:
            return self._llmFactory(None)
        except Exception:  # noqa: BLE001 —— 客户端创建失败降级为「无 LLM」，不中断 turn
            logger.error("LLM 客户端创建失败，本次降级跳过 LLM 段", exc_info=True)
            return None

    @staticmethod
    def _clientModelName(client: Any) -> str | None:
        """客户端固化的模型名快照（OpenAiClient._modelName，openai_client.py:75）。"""
        return getattr(client, "_modelName", None) or getattr(client, "model_name", None)

    async def _recordUsage(
        self,
        session: AsyncSession,
        *,
        sessionId: uuid.UUID,
        purpose: str,
        promptTokens: int,
        completionTokens: int,
        modelName: str | None,
    ) -> None:
        """记一次 LLM 用量；记账失败只留痕（不能把已发生的消耗变成未知）。

        零消耗直接跳过（模板降级 / 未真正调 LLM 的相位不写空台账行）。
        """
        if promptTokens + completionTokens <= 0:
            return
        try:
            await self._usage.recordUsage(
                session,
                sessionId=str(sessionId),
                purpose=purpose,
                promptTokens=promptTokens,
                completionTokens=completionTokens,
                modelName=modelName,
            )
        except Exception:  # noqa: BLE001 —— 与 chat `_chartStep` 同处置：记账失败不带走整轮
            logger.error(
                "研究链路用量记账失败: session=%s purpose=%s pt=%s ct=%s",
                sessionId,
                purpose,
                promptTokens,
                completionTokens,
                exc_info=True,
            )

    @staticmethod
    async def _rollbackAfterStepFailure(session: AsyncSession) -> None:
        """执行步失败后清障：不回滚则后续语句一律 PendingRollbackError。"""
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 —— 连接已死时兜底，原错误更值得上抛
            logger.warning("执行步失败后的 rollback 异常", exc_info=True)

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

    @staticmethod
    async def _emit(emit: Emit | None, event: str, payload: dict[str, Any]) -> None:
        """推进度事件；SSE 通道故障只留痕（状态已在 DB，不因推流失败中断 turn）。"""
        if emit is None:
            return
        try:
            await emit(event, payload)
        except Exception:  # noqa: BLE001 —— 推流失败不改变 turn 结果
            logger.warning("研究进度事件发送失败: event=%s", event, exc_info=True)
