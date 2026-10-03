"""研究状态机的共享词汇、端口适配器与无状态构件（Task 5 fix round 1 抽取）。

`research_agent_service.py` 只留编排（相位调度 + IO）；本模块承载三块：

1. **共享词汇**：相位 / checkpoint 相位 / 动态信号 / options 键 / 会话状态 / 动作映射 /
   计量 purpose / SSE 事件名（含 `research.error`）。`PHASES` 由服务模块再导出，
   保持 brief 的 `from app.services.research_agent_service import PHASES` 契约。
2. **端口与默认适配器**：`UsageRecorder` + `LlmUsageRecorder` + `MeteredClient`（计量）、
   `Reporter` + `DefaultReporter`（报告；Task 6 落地后由真实 ReportPlanner 替换）。
3. **无状态构件**：state 重建 / options 构造 / 相位映射 / 步结果与计划归一化 /
   假设筛选与打分 / 提示词取值助手 / 静默 emit 与 rollback 兜底。

抽取动因：服务文件曾 1072 行，超 800 行硬上限；行为零变化（同一批测试全绿）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.domain.research_models import ResearchFinding, ResearchSession
from app.services.token_usage_service import TokenUsageService

logger = logging.getLogger(__name__)

# --- 相位与 checkpoint 相位（落 research_checkpoint.phase，长度上限 30）-------
PHASES = ("intent", "esl", "plan", "execute", "hypothesis", "verify", "report")
"""状态机相位（顺序即执行顺序）。"""
PHASE_ESL = "esl"
"""空 scope 改写通道强制重跑的相位。"""

CHECKPOINT_INTENT = "intent"  # 固定 #1 三臂范围确认（由 _stageEsl 开启）
CHECKPOINT_PLANNING = "planning"  # 固定 #2 计划确认（由 _stagePlan 开启）
CHECKPOINT_HYPOTHESIS = "hypothesis"  # 固定 #3 假设挑选（由 _stageHypothesis 开启）
CHECKPOINT_RUNTIME_DYNAMIC = "runtime_dynamic"  # 动态：ESL 冲突，决策后直进 plan
CHECKPOINT_LOW_CONFIDENCE = "low_confidence_step"  # 动态：执行步失败/空数据

# 固定 checkpoint 的恢复点（**兜底**）；权威恢复点是 options["resumePhase"]，见
# nextPhaseForPhase。动态 checkpoint 没有表项，只能走 options。
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

# --- options 键名（OPT_RESUME_PHASE 是恢复点 SSOT，显式值优先于固定表）--------
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

# --- SSE 事件名（设计 §4.5）/ 错误码 / 默认 mode ------------------------------
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
EVENT_ERROR = "research.error"
"""错误事件（设计 §4.5：`{code, message}`）；降级与失败都必须显式可见，不静默。"""

ERROR_LLM_UNAVAILABLE = "llm_unavailable"
ERROR_TURN_FAILED = "turn_failed"
ERROR_HYPOTHESIS_FAILED = "hypothesis_generation_failed"
LLM_UNAVAILABLE_MESSAGE = "无可用 LLM 客户端：本轮 LLM 段降级（计划/假设/报告文本可能不完整）"
DEFAULT_MODE = "research"

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
Pause = tuple[str, dict[str, Any], str]  # (checkpoint 相位, options, 提示文案)


# ---------------------------------------------------------------------------
# 端口协议 + 默认适配器
# ---------------------------------------------------------------------------


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


class MeteredClient:
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


class DefaultReporter:
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


# ---------------------------------------------------------------------------
# 无状态构件：state 重建 / options / 相位映射
# ---------------------------------------------------------------------------


def buildOptions(*, signal: str, resumePhase: str, **payload: Any) -> dict[str, Any]:
    """checkpoint options：信号 + 恢复相位 + 语义载荷（前端卡片直接渲染）。"""
    return {
        OPT_SIGNAL: signal,
        OPT_RESUME_PHASE: resumePhase,
        **{key: value for key, value in payload.items() if value is not None},
    }


def resumeTurnContent(
    *, action: str, choice: dict[str, Any], checkpointId: uuid.UUID, rewritten: str
) -> dict[str, Any]:
    """恢复轮的 user turn 内容；带改写问题时一并记录新问题（可追溯）。"""
    content: dict[str, Any] = {
        "action": action,
        "choice": choice or {},
        "checkpointId": str(checkpointId),
    }
    if rewritten:
        content["question"] = rewritten
    return content


def nextPhaseForPhase(phase: str, options: dict[str, Any]) -> str:
    """checkpoint 相位 → 恢复相位：`options["resumePhase"]` 是 SSOT（显式值优先）。

    显式优先是空 scope 改写通道的前提：该 checkpoint 的 phase 是 `intent`（固定 #1 的
    相位名，表里映射到 plan），只有显式 `resumePhase="esl"` 才能回到 ESL 重跑。
    显式值缺失时才查固定表，再退到末相位。
    """
    explicit = str(options.get(OPT_RESUME_PHASE) or "")
    if explicit:
        return explicit
    return NEXT_PHASE_BY_CHECKPOINT.get(phase) or PHASES[-1]


def nextPhase(checkpoint: Any, action: str) -> str:
    """决策后回到哪个相位；动态点 reject 时走 `abortPhase`（终止到出报告）。"""
    options = checkpoint.options or {}
    if action == ACTION_REJECT and options.get(OPT_ABORT_PHASE):
        return str(options[OPT_ABORT_PHASE])
    return nextPhaseForPhase(checkpoint.phase, options)


def rebuildState(row: ResearchSession, checkpoint: Any) -> dict[str, Any]:
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


def rewriteState(state: dict[str, Any], question: str) -> dict[str, Any]:
    """改写问题后的恢复态：换问题 + 清空派生数据（强制重跑 ESL，旧三臂不得带进新问题）。"""
    return {
        **state,
        "question": question,
        "esl": None,
        "plan": None,
        "stepResults": [],
        "hypotheses": [],
        "resumeStepIndex": 0,
    }


def stepSignal(error: str | None) -> str:
    """步失败信号名：SQL Guard 拒绝单列，其余归 low_confidence_step（设计 §4.8）。"""
    if error and error.startswith(SIGNAL_SQL_VALIDATION_FAILED):
        return SIGNAL_SQL_VALIDATION_FAILED
    return SIGNAL_LOW_CONFIDENCE_STEP


# ---------------------------------------------------------------------------
# 无状态构件：计划 / 步结果归一化
# ---------------------------------------------------------------------------


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


def stepResult(
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


# ---------------------------------------------------------------------------
# 无状态构件：假设筛选与打分 / 提示词取值助手
# ---------------------------------------------------------------------------


def selectedHypotheses(state: dict[str, Any]) -> list[dict[str, Any]]:
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


def candidateConfidence(state: dict[str, Any], candidate: dict[str, Any]) -> float:
    """候选分：假设 driver 命中 ESL 指标/本体时的置信度；未命中取基线。"""
    driver = str(candidate.get("driver") or "").strip().lower()
    if driver:
        for name, score in confidenceIndex(state):
            if driver in name.lower():
                return score
    return DEFAULT_CANDIDATE_CONFIDENCE


def confidenceIndex(state: dict[str, Any]) -> list[tuple[str, float]]:
    esl = state.get("esl") or {}
    pairs: list[tuple[Any, Any]] = []
    for metric in esl.get("metrics") or []:
        pairs += [(metric.get("displayName"), metric.get("confidence"))]
        pairs += [(metric.get("kpiCode"), metric.get("confidence"))]
    for obj in esl.get("businessObjects") or []:
        pairs += [(obj.get("className"), obj.get("confidence"))]
        pairs += [(obj.get("matchedAlias"), obj.get("confidence"))]
    return [(str(name), float(score)) for name, score in pairs if name and score is not None]


def drivers(state: dict[str, Any]) -> list[str]:
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


def dataSummary(state: dict[str, Any]) -> str:
    """喂给假设生成的执行摘要（只用步摘要，不塞原始行）。"""
    lines = [
        f"步骤 {int(r.get('index', 0)) + 1} {r.get('description') or ''}：{r.get('summary') or ''}"
        for r in state.get("stepResults") or []
    ]
    return "\n".join(lines) or "（本轮未取到数据）"


def eslClasses(state: dict[str, Any]) -> list[str]:
    """喂给计划器的本体类提示：ESL 命中 BO 的物理表名。"""
    esl = state.get("esl") or {}
    return [str(o["sourceTable"]) for o in esl.get("businessObjects") or [] if o.get("sourceTable")]


def clientModelName(client: Any) -> str | None:
    """客户端固化的模型名快照（OpenAiClient._modelName，openai_client.py:75）。"""
    return getattr(client, "_modelName", None) or getattr(client, "model_name", None)


# ---------------------------------------------------------------------------
# 无状态构件：静默 emit / rollback 兜底
# ---------------------------------------------------------------------------


async def emitEvent(emit: Emit | None, event: str, payload: dict[str, Any]) -> None:
    """推进度事件；SSE 通道故障只留痕（状态已在 DB，不因推流失败中断 turn）。"""
    if emit is None:
        return
    try:
        await emit(event, payload)
    except Exception:  # noqa: BLE001 —— 推流失败不改变 turn 结果
        logger.warning("研究进度事件发送失败: event=%s", event, exc_info=True)


async def rollbackQuietly(session: AsyncSession) -> None:
    """清障回滚：语句失败会让 session 进入失败事务态，不回滚则后续一律 PendingRollbackError。

    rollback 自身再失败（连接已死）只 warning，不上抛——原始错误更值得上抛。
    """
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 —— 连接已死时兜底
        logger.warning("研究链路 rollback 异常", exc_info=True)
