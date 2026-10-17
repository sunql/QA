"""研究入口 turn 状态机（feat-research-entry Task 5）。

设计 §4.3 的 12 步压缩为 7 个相位：intent → esl → plan → execute → hypothesis →
verify → report。三个**固定 checkpoint**（范围确认 / 计划确认 / 假设挑选）与两个
**动态相位**（`runtime_dynamic`：ESL 冲突；`low_confidence_step`：执行步失败或空数据）
在指定相位暂停：状态落 `research_checkpoint`（`options` 带恢复所需的语义载荷），
用户决策后 `resumeTurn` 从「下一相位」续跑。

本模块只留编排（相位调度 + IO）；7 个相位执行体与相位纯函数在
`research_agent_phases.py`（Task 9 Step 0 抽取，零行为变化），共享词汇、端口协议、默认适配器
与无状态构件在 `research_agent_ports.py`（fix round 1 抽取），执行面（步 SQL 生成 → 只读查询 →
出图）在 `research_agent_execution.py`（Task 6.5 fix round 2 抽取）——都是为守住 800 行硬上限。
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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchCheckpoint, ResearchSession
from app.services.intent_service import IntentService
from app.services.model_config_service import ModelConfigService
from app.services.model_router_service import ModelRouterService
from app.services.ontology_service import OntologyService
from app.services.report_planner import ReportPlanner
from app.services.research_agent_execution import ExecutionDeps, buildExecutionDeps
from app.services.research_agent_model_resolution import resolveClient
from app.services.research_agent_phases import (
    ResearchAgentPhasesMixin,
    decideResume,
    isDegraded,
    resumeState,
    rewrittenQuestion,
)
from app.services.research_agent_ports import (
    ACTION_STATUS,
    ERROR_HYPOTHESIS_FAILED,
    ERROR_TURN_FAILED,
    EVENT_CHECKPOINT,
    EVENT_DONE,
    EVENT_ERROR,
    OPT_REPLAN_COUNT,
    PHASE_ESL,
    PHASE_EXECUTE,
    PHASE_HYPOTHESIS,
    PHASE_INTENT,
    PHASE_PLAN,
    PHASE_REPORT,
    PHASE_VERIFY,
    PHASES,
    PURPOSE_HYPOTHESIS,
    ROLE_CHECKPOINT,
    ROLE_USER,
    STATUS_AWAITING,
    STATUS_DONE,
    STATUS_RUNNING,
    Emit,
    LlmUsageRecorder,
    MeteredClient,
    Pause,
    Reporter,
    SessionBudgetExceeded,
    UsageRecorder,
    emitEvent,
    errorPayload,
    nextPhaseForPhase,
    recordUsageQuietly,
    requireQuestion,
)
from app.services.research_agent_stages import (
    dataSummary,
    drivers,
    meteringModelName,
    resumeTurnContent,
    rewriteState,
)
from app.services.research_run_state import RunStateSeed, buildRunState, stateToDict, updateRunState
from app.services.research_hypothesis_adapter import generateHypotheses
from app.services.research_session_service import (
    RUN_STATE_IDEMPOTENCY_KEY,
    TERMINAL_SESSION_STATUSES,
    CheckpointConflictError,
    ResearchSessionService,
    RunClaimConflictError,
)
from app.services.token_usage_service import TokenUsageService
from app.config import getSettings

logger = logging.getLogger(__name__)

__all__ = ["PHASES", "ResearchAgentService"]

SESSION_BUDGET_MESSAGE_DECIMALS = 6
"""终止文案里成本/预算的**定点**显示位数（Decimal 定点格式化，杜绝 `1E-2` 科学计数法）。"""

SESSION_BUDGET_HINT = "可新建会话或调高 SESSION_BUDGET 后重试"
"""超预算终止文案的处置提示（用户可执行的一步；与路由器**降级**口径刻意区分）。"""

SESSION_BUDGET_EXCEEDED_MESSAGE = f"会话累计成本已达预算上限，本轮研究终止；{SESSION_BUDGET_HINT}"
"""读不到真实金额时的兜底终止文案（二次读打嗝，近乎不可达；仍显式给处置）。"""


def sessionBudgetMessage(cost: Decimal, budget: Decimal) -> str:
    """超预算终止文案：带**真实**成本与预算（定点），并给出用户可执行的处置。"""
    return (
        f"会话累计成本 ${cost:.{SESSION_BUDGET_MESSAGE_DECIMALS}f}"
        f" 已达预算上限 ${budget:.{SESSION_BUDGET_MESSAGE_DECIMALS}f}，"
        f"本轮研究终止；{SESSION_BUDGET_HINT}"
    )


class ResearchAgentService(ResearchAgentPhasesMixin):
    """研究 turn 状态机（无状态；构造注入协作者）。

    7 个相位执行体（`_stageXxx`）在 `ResearchAgentPhasesMixin`（Task 9 Step 0 抽出）；
    本类只留编排与公开面。
    """

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
        sessionBudget: float | None = None,
    ) -> None:
        self._esl = esl
        self._sessions = sessionService
        self._planner = planner
        self._runner = runner
        self._llmFactory = llmFactory
        self._usage = usageRecorder if usageRecorder is not None else LlmUsageRecorder()
        self._autoConfirm = autoConfirm
        # 会话级预算闸门的**可注入** seam（Task 15）：None ⇒ 用时读 `getSettings().sessionBudget`。
        # 生产构造点不传（走全局配置）；测试注入以免污染全局 settings 缓存。
        self._sessionBudget = sessionBudget
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
        """执行面依赖（Task 6.5 抽到 research_agent_execution）；构造见 `buildExecutionDeps`。"""
        return buildExecutionDeps(
            runner, chartService, self._ontology, nl2sql, self._resolveClient, self._recordUsage
        )

    def _buildStages(self) -> dict[str, Any]:
        """相位调度表（键 = `PHASES` 的元素，顺序由 `PHASES` 决定）。"""
        return {
            PHASE_INTENT: self._stageIntent,
            PHASE_ESL: self._stageEsl,
            PHASE_PLAN: self._stagePlan,
            PHASE_EXECUTE: self._stageExecute,
            PHASE_HYPOTHESIS: self._stageHypothesis,
            PHASE_VERIFY: self._stageVerify,
            PHASE_REPORT: self._stageReport,
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
            session, sessionId=sessionId, role=ROLE_USER, content={"question": question}
        )
        logger.info("研究 turn 开始: session=%s turn=%s userId=%s", sessionId, turn.id, userId)
        return await self.runTurn(
            session, sessionId=sessionId, turnId=turn.id, question=question,
            userId=userId, mode=row.mode, emit=emit,
        )

    async def runTurn(
        self, session: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID,
        question: str, userId: int | None, mode: str, emit: Emit | None = None,
        idempotencyKey: str | None = None,
    ) -> str:
        """从**已落库的 user turn** 起跑状态机；返回 `awaiting_user` 或 `done`。

        与 `startTurn` 的唯一差别是「谁写 user turn」：API 层必须先拿到 turnId 才能
        立刻回 202（Task 7），故 user turn 由调用方写入后经本方法续跑；`startTurn`
        即 `appendTurn` + 本方法的等价组合（行为不变，Task 7 抽取）。

        Task 7.5 LOW：空白问题守卫下沉到此处（API 路径 `createTurn → appendTurn →
        runTurn` 绕过 `startTurn`）；守卫在任何写/状态推进之前，空问题不推进状态机。

        Task 2：`idempotencyKey` 落 `state_payload`（重试回放同一 turnId 的查据），
        并据此**原子认领会话执行权** —— 未抢到（另一条状态机在跑）抛
        `RunClaimConflictError`，由后台 wrapper 转成 SSE 冲突事件（不落 failed）。
        """
        requireQuestion(question, sessionId)
        # 会话级模型选择（W5）：每轮从会话行取，保证追问也沿用同一模型。
        row = await self._loadSession(session, sessionId)
        run_state = buildRunState(RunStateSeed(
            session_id=sessionId,
            turn_id=turnId,
            current_question=question,
            mode=mode,
            user_id=userId,
            model_id=row.model_id,
            datasource_id=row.datasource_id,
        ))
        if idempotencyKey:
            run_state.state_payload = {
                **run_state.state_payload,
                RUN_STATE_IDEMPOTENCY_KEY: idempotencyKey,
            }
        await self._sessions.createRunState(session, run_state)
        # active_run_id 有 FK 到 research_run_state：先落地 run 行再认领
        await session.flush()
        claimed, existing = await self._sessions.claimActiveRun(
            session, sessionId, run_state.id, idempotencyKey
        )
        if not claimed:
            raise RunClaimConflictError(
                f"会话已有进行中的研究轮次: session={sessionId} run={existing}"
            )
        if existing is not None:
            # 幂等键命中**另一条**运行（同键双提交的竞态窗口）：本次不跑，让已占坑那条推进。
            # 正常重试走不到这里 —— API 层先回放首次 turnId、不再排后台任务；只有两条后台
            # 任务同时起跑才会撞上。放行会让同一会话并行跑两条状态机（互斥形同失效）。
            raise RunClaimConflictError(
                f"幂等键已被另一运行占用: session={sessionId} key={idempotencyKey} run={existing}"
            )
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_RUNNING)
        state: dict[str, Any] = {
            **stateToDict(run_state),
            "runStateId": run_state.id,
            "esl": None,
            "plan": None,
            "stepResults": [],
            "hypotheses": [],
            "resumeStepIndex": 0,
            "choice": {},
            "llmUnavailable": False,
        }
        return await self._guardedRun(
            session, sessionId=sessionId, turnId=turnId,
            phase=PHASES[0], emit=emit, state=state,
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
        # B5：先加载 checkpoint 拿到 session_id，再加载 session 获取 userId（audit_log actor）
        cp_row = await session.get(ResearchCheckpoint, checkpointId)
        if cp_row is None:
            raise ValueError(f"checkpoint 不存在: {checkpointId}")
        session_row = await self._loadSession(session, cp_row.session_id)
        # 终态会话不可续跑（Task 2 / review Fix F）。**这是让 abort 成为「最终」的那道闸门**：
        # abort 只翻会话状态，pending checkpoint 行仍留着 —— 迟到的「答卡片」会把它 resolve
        # 掉、本方法再把状态写回 `running` ⇒ 会话复活。更糟的是本方法**从不认领执行权**，
        # 复活后的会话是 `running` + `active_run_id IS NULL` ⇒ 并发的 `POST /turns` 直接放行
        # ⇒ 同一会话两条状态机（正是本任务的互斥要防的事）。
        # 守卫放在 `resolveCheckpoint` **之前**：一行 checkpoint 都不改、状态也不写，
        # 「被拒绝」与「库里没动过」永远是同一件事。
        if session_row.status in TERMINAL_SESSION_STATUSES:
            logger.warning(
                "拒绝续跑终态会话: session=%s status=%s checkpoint=%s",
                cp_row.session_id,
                session_row.status,
                checkpointId,
            )
            raise CheckpointConflictError(
                f"会话已结束（{session_row.status}），无法再续跑: session={cp_row.session_id}"
            )
        checkpoint = await self._sessions.resolveCheckpoint(
            session,
            checkpointId=checkpointId,
            status=status,
            userChoice=choice or {},
            userId=session_row.created_by,
        )
        # 通过 checkpoint.turn_id 找到对应 run state；校验归属防止 URL/ID 拼写错位
        run_state = await self._sessions.getRunStateByTurn(session, checkpoint.turn_id)
        if run_state is None or run_state.session_id != checkpoint.session_id:
            raise ValueError(f"checkpoint 对应的 run state 不存在: checkpoint={checkpointId}")

        rewritten = await self._recordResumeTurn(
            session, checkpoint, action=action, choice=choice, checkpointId=checkpointId
        )
        # 会话级模型选择（W5）：checkpoint 载荷不带 modelId（模型不是落库状态），由 resumeState 补回。
        state = resumeState(run_state, checkpoint, choice)
        decision = decideResume(checkpoint, action, choice)
        startPhase = decision.phase
        run_state, state = await self._applyResumeMutations(
            session, run_state, decision=decision, rewritten=rewritten, state=state
        )
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

    async def _applyResumeMutations(
        self,
        session: AsyncSession,
        run_state: Any,
        *,
        decision: Any,
        rewritten: str,
        state: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        """按续跑决策改写状态机态并同步落库版本化 run state。

        返回 (最新 run_state, 最新 state)：rewriteState 会返回新 dict，故二者都要回传。

        单事务内最多写一行新版本：replan 计数与改写问题合并为一次 updateRunState。
        （decideResume 保证改写优先于 replan、二者互斥，但合写一行是结构性保证——
        同 tx 两行快照的 created_at 并列时，getRunStateByTurn 的 tie-break 是随机 UUID，
        “最新行”不确定，可能取回改写前的旧问题。）
        """
        newPayload: dict[str, Any] | None = None
        if decision.isReplan:
            # Task 6.5-4（MEDIUM-8）：计划点的 modify 不是「确认旧计划继续跑」，而是带
            # 用户反馈**重跑 planner**；故回到 plan 相位并置 replan 标记。2026-10-11 裁定：
            # 第 1..MAX 次 modify 会**重新暂停**在计划点（用户看得到新计划）。
            # Task B1：replan 上限由 decideResume 封顶；达到上限后它返 execute、不置 isReplan。
            state["replan"] = True
            state[OPT_REPLAN_COUNT] = int(state.get(OPT_REPLAN_COUNT, 0)) + 1
            newPayload = {
                **(run_state.state_payload or {}),
                OPT_REPLAN_COUNT: state[OPT_REPLAN_COUNT],
            }
        newQuestion: str | None = None
        newScopeVersion: int | None = None
        if rewritten:
            state = rewriteState(state, rewritten)
            newQuestion = rewritten
            # rewriteState 递增后的 scopeVersion 同步落库，跨多轮恢复不回退
            newScopeVersion = int(state.get("scopeVersion") or 1)
        if newPayload is not None or newQuestion is not None:
            # 同步落库：改写后的问题/scope 版本/replan 计数成为本轮当前版本
            run_state = updateRunState(
                run_state,
                current_question=newQuestion,
                scope_version=newScopeVersion,
                state_payload=newPayload,
            )
            await self._sessions.updateRunState(session, run_state)
        # 版本化后 run_state.id 可能已变，状态机携带的标识必须同步
        state["runId"] = str(run_state.id)
        state["runStateId"] = run_state.id
        return run_state, state

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
        rewritten = rewrittenQuestion(choice)
        content = resumeTurnContent(
            action=action, choice=choice, checkpointId=checkpointId, rewritten=rewritten
        )
        await self._sessions.appendTurn(
            session, sessionId=checkpoint.session_id, role=ROLE_USER, content=content
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
        """跑状态机；异常时发 `research.error`、把会话标 failed，再原样上抛（不吞错）。

        Task 2：`finally` 里释放会话执行权（`releaseActiveRun`）。**只有终态才真释放** ——
        暂停（awaiting_user）时状态机正常返回，执行权必须留着，否则并发轮次会溜进来。
        """
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
                errorPayload(ERROR_TURN_FAILED, str(exc), phase=phase),
            )
            await self.markFailed(session, sessionId)
            raise
        finally:
            await self._releaseClaim(session, sessionId, state)

    async def _releaseClaim(
        self, session: AsyncSession, sessionId: uuid.UUID, state: dict[str, Any]
    ) -> None:
        """收尾释放会话执行权（best-effort：释放失败不得盖住原始异常或正常返回值）。

        真正生效只在**会话终态**（`releaseActiveRun` 内部按状态判定）：暂停等用户决策时
        状态机是正常返回，执行权必须保留给同一条运行的后续 resume。
        失败路径上本调用会被后台 wrapper 的 rollback 撤掉 —— 由 `markFailed` 重放释放。
        """
        runId = state.get("runStateId")
        try:
            await self._sessions.releaseActiveRun(session, sessionId, runId)
        except Exception:  # noqa: BLE001 —— 收尾释放失败只留痕（同 markFailed 口径）
            logger.error("释放 active_run 失败: session=%s run=%s", sessionId, runId, exc_info=True)

    # --- 会话级预算只读闸门（Task 15）：相位边界查累计成本，超限即终止本轮（不换模型）---

    def _effectiveSessionBudget(self) -> Decimal:
        """生效预算（注入值优先，否则 `getSettings().sessionBudget`）；经 `str` 转 `Decimal`
        以挡住 float 二进制噪声 —— 金额比较全程 `Decimal`，绝不退化成 float。"""
        raw = self._sessionBudget if self._sessionBudget is not None else getSettings().sessionBudget
        return Decimal(str(raw))

    async def _readSessionCost(self, session: AsyncSession, sessionId: uuid.UUID) -> Decimal | None:
        """读会话累计成本（best-effort）；读失败返回 None（计量打嗝不得杀研究）。"""
        try:
            cost = await self._tokenUsage.getSessionCost(session, str(sessionId))
        except Exception:  # noqa: BLE001 —— 与 buildRoutingContext 同口径：读用量失败不阻断
            logger.warning("读取会话累计成本失败，预算闸门本次跳过: session=%s", sessionId, exc_info=True)
            return None
        return cost if isinstance(cost, Decimal) else Decimal(str(cost))

    async def _sessionBudgetExceeded(self, session: AsyncSession, sessionId: uuid.UUID) -> bool:
        """会话累计成本已达/超预算 ⇒ True（本轮研究在**相位边界**终止；**绝不**选/换模型）。

        预算语义（**与路由器相反，刻意如此**）：`budget <= 0` ⇒ **关闭闸门**（恒 False）。
        路由器 `ModelRouterService.selectModel` 用 `>=`，把 `SESSION_BUDGET=0` 当作「必然
        超预算 ⇒ 降级到最便宜模型」—— 同一配置值两处含义相反。闸门需要「能整关」的语义，
        故按 `<= 0` 关闭实现（本 Task **不改**路由器；分歧登记为遗留，见任务回报）。

        best-effort：读成本失败 ⇒ False（计量打嗝既不得杀研究，也不得误判为超限）。
        """
        budget = self._effectiveSessionBudget()
        if budget <= 0:
            return False
        cost = await self._readSessionCost(session, sessionId)
        return cost is not None and cost >= budget

    async def _sessionBudgetMessage(self, session: AsyncSession, sessionId: uuid.UUID) -> str:
        """超预算终止文案（读**真实**成本/预算）——仅在 `_sessionBudgetExceeded` 为真后调用。"""
        cost = await self._readSessionCost(session, sessionId)
        if cost is None:  # 二次读打嗝：仍终止，文案退化为不含金额的兜底（近乎不可达）
            return SESSION_BUDGET_EXCEEDED_MESSAGE
        return sessionBudgetMessage(cost, self._effectiveSessionBudget())

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
        """从 `phase` 起顺序跑各相位；任一相位返回暂停信号即落 checkpoint 并返回。

        Task 15：**相位边界**先过会话预算只读闸门（不半途打断一条相位）—— 超限即抛
        `SessionBudgetExceeded`，由 `_guardedRun` 转成 `research.error{turn_failed}` +
        `markFailed`（复用既有终态路径，**不**新增 error code）。
        """
        for current in PHASES[PHASES.index(phase):]:
            if await self._sessionBudgetExceeded(session, sessionId):
                raise SessionBudgetExceeded(
                    await self._sessionBudgetMessage(session, sessionId)
                )
            pause = await self._stages[current](
                session, sessionId=sessionId, turnId=turnId, emit=emit, state=state
            )
            if pause is not None:
                await self._pauseForUser(
                    session, sessionId=sessionId, turnId=turnId, pause=pause, emit=emit, state=state
                )
                return STATUS_AWAITING
        await self._sessions.updateSessionStatus(session, sessionId, STATUS_DONE)
        await emitEvent(
            emit,
            EVENT_DONE,
            {
                "sessionId": str(sessionId),
                "reportId": state.get("reportId"),
                "degraded": isDegraded(state),
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
        state: dict[str, Any] | None = None,
    ) -> None:
        """落 checkpoint + checkpoint_awaiting turn + 会话置 awaiting_user。

        X3（1012 cross-refs spec §P0-X3）：把当轮研究问题固化到 checkpoint 本体
        （openCheckpoint 形参 ``originalQuestion``），恢复阶段优先用本列而不再
        join turn.content。``state`` 可选 — 旧调用方（无 state）传空串，
        行为不破坏既有数据，恢复层继续用 turn.content 老路径兜底。
        """
        phase, options, prompt = pause
        settings = getSettings()
        expires_at = datetime.now(UTC) + timedelta(hours=settings.researchCheckpointTtlHours)
        original_question = (state or {}).get("question") or ""
        checkpoint = await self._sessions.openCheckpoint(
            session,
            sessionId=sessionId,
            turnId=turnId,
            phase=phase,
            options=options,
            prompt=prompt,
            originalQuestion=original_question,
            expiresAt=expires_at,
        )
        await self._sessions.appendTurn(
            session,
            sessionId=sessionId,
            role=ROLE_CHECKPOINT,
            content={
                "checkpointId": str(checkpoint.id),
                "phase": phase,
                # 仅作展示/调试用途：恢复点 SSOT 是 `decideResume`（联合 checkpoint.phase /
                # action / choice / options 判定），不是本字段
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
            # R1（2026-10-09）：研究侧 schema 反射（真表 + 真列 + 值域采样）喂给假设 LLM。
            # 让 LLM 看到 ``DWD_GOODS_RECEIPT_DTL(rcv_qty, ...)`` 而不是只看到"供应商"。
            # 失败面**已**在 buildHypothesisSchemaHint 内部全降级（无源/无 schema/采样失败
            # 都返占位符），外层 try 仅守"连占位符都构造不出来"这种编程错误。
            schemaHint = ""
            esl = state.get("esl")
            if isinstance(esl, dict) and esl.get("businessObjects"):
                from app.services.research_schema_reflection import (
                    buildHypothesisSchemaHint,
                )
                try:
                    schemaHint = await buildHypothesisSchemaHint(
                        session, sessionId, state["esl"],
                    )
                except Exception:
                    logger.warning(
                        "研究侧 schema 反射失败，降级空 hint: session=%s",
                        sessionId, exc_info=True,
                    )
                    schemaHint = ""
            hypotheses = await generateHypotheses(
                metered, state["question"], dataSummary(state), drivers(state),
                schemaHint=schemaHint,
            )
            candidates = [asdict(h) for h in hypotheses]
        except Exception:  # noqa: BLE001 —— 假设生成失败降级为空候选（研究链路不因它中断）
            logger.error("假设生成失败，降级为空候选: session=%s", sessionId, exc_info=True)
            await emitEvent(
                emit,
                EVENT_ERROR,
                errorPayload(ERROR_HYPOTHESIS_FAILED, "假设生成失败，本轮无候选假设"),
            )
        await self._recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_HYPOTHESIS,
            promptTokens=metered.promptTokens,
            completionTokens=metered.completionTokens,
            modelName=meteringModelName(client, metered),
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
        """取（客户端, 模型配置）二元组：步 SQL 生成需要配置透传给 NL2SQL。

        会话级模型选择（W5-b）从 `state["modelId"]` 读（调用点不传参数）；显式选定后
        不可用会由 `resolveModelConfig` 抛 `PreferredModelUnavailableError`，不静默换模型。
        """
        return await resolveClient(
            self._llmFactory,
            self._modelConfigs,
            self._modelRouter,
            session,
            state=state,
            emit=emit,
            sessionId=sessionId,
            tokenUsage=self._tokenUsage,
            preferredModelId=state.get("modelId"),
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

    async def markFailed(self, session: AsyncSession, sessionId: uuid.UUID) -> None:
        """把会话标 failed **并释放执行权**（公开面：`_guardedRun` 与 API 后台 wrapper 共用；
        best-effort 不抛二次异常）。

        Task 2：failed 是终态 ⇒ 执行权必须一并释放，否则会话会带着 active_run_id 永久
        409（新轮次再也提交不进来）。释放**必须**在这里、而不只在 `_guardedRun` 的 finally：
        后台 wrapper 捕获异常后会 `rollback`，把 finally 里那次释放一并撤掉，然后重放
        本方法 —— 只有在同一次重放里释放，终态与释放才原子地一起落下。

        落终态与释放走 `failSession` 的**单条 UPDATE**（不是「先 updateSessionStatus 再
        releaseActiveRun」）：两步之间任一步失败都留下「非终态 + 占用中」的锁死态。
        """
        try:
            await self._sessions.failSession(session, sessionId)
        except Exception:  # noqa: BLE001 —— 失败态落库失败不能盖住原始异常
            logger.error("标记会话失败态失败: session=%s", sessionId, exc_info=True)

    async def _loadSession(self, session: AsyncSession, sessionId: uuid.UUID) -> ResearchSession:
        row = await session.get(ResearchSession, sessionId)
        if row is None:
            logger.warning("研究会话不存在: id=%s", sessionId)
            raise ValueError(f"研究会话不存在: {sessionId}")
        return row
