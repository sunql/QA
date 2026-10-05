"""研究状态机的共享词汇、端口适配器与无状态构件（Task 5 fix round 1 抽取）。

`research_agent_service.py` 只留编排（相位调度 + IO）；本模块承载三块：

1. **共享词汇**：相位 / checkpoint 相位 / 动态信号 / options 键 / 会话状态 / 动作映射 /
   计量 purpose / SSE 事件名（含 `research.error`）。`PHASES` 由服务模块再导出，
   保持 brief 的 `from app.services.research_agent_service import PHASES` 契约。
2. **端口与默认适配器**：`UsageRecorder` + `LlmUsageRecorder` + `MeteredClient`（计量）、
   `Reporter`（报告端口；Task 6 起唯一实现是 `ReportPlanner`，占位实现已删除）。
3. **无状态构件**：options 构造 / 相位映射（`nextPhaseForPhase`）/ 步结果与计划归一化 /
   静默 emit 与 rollback 兜底。（恢复态重建 / 改写态 / 恢复轮内容、假设筛选与打分 /
   提示词取值助手见 `research_agent_stages.py`，Task 8 / 8.5 抽出；`nextPhase` /
   `isDegraded` / `findingData` 与 7 个相位执行体见 `research_agent_phases.py`，
   Task 9 Step 0 抽出 —— 都是为守住 800 行硬上限。）

抽取动因：服务文件曾 1072 行，超 800 行硬上限；行为零变化（同一批测试全绿）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE
from app.services.model_router_service import RoutingContext
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
OPT_STEPS_EXECUTED = "stepsExecuted"
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
PURPOSE_REPORT = "research_report"
"""报告文本块的 LLM 用量 purpose（Task 6；一次 compose 会逐块调用，见 MeteredClient）。"""
PURPOSE_CHART = "research_chart"
"""执行步出图的 LLM 用量 purpose（Task 6.5：ChartService 调用也必须走计量客户端）。"""
LLM_CACHE_HIT_MULTIPLIER_KEY = "LLM_CACHE_HIT_MULTIPLIER"
"""prompt-cache 命中折扣常数的 system_config 键（SSOT 与 chat 同一行，见 _cacheHitMultiplier）。"""
VERIFY_OK_FACTOR = 0.9  # 验证成功：候选分 × 0.9
VERIFY_FAIL_FACTOR = 0.3  # 验证失败：候选分 × 0.3（失败本身是合法结论，不是 0）
DEFAULT_CANDIDATE_CONFIDENCE = 0.5  # 假设 driver 未命中 ESL 候选时的基线分
MAX_VERIFY_HYPOTHESES = 3
DRIVER_HINT_LIMIT = 30
COLUMN_SUMMARY_LIMIT = 12
STEP_MISSING_SQL = "计划步未携带 SQL（逐步 NL2SQL 生成失败或未接线）"

# --- finding.supporting_data 契约（Task 5 写 / Task 6 报告读）------------------
FINDING_ROWS_KEY = "rows"
"""验证结果行数据。报告 chart/table 块的**唯一**数据源，逐字透传（Task 6 §4.7 偏差）。"""
MAX_FINDING_ROWS = 100
"""落库行上限：bound JSONB 体积；超出丢弃并 warning（不静默截断）。"""

# --- SSE 事件名（设计 §4.5）/ 错误码 / 默认 mode ------------------------------
EVENT_INTENT = "research.intent"
EVENT_ESL = "research.esl"
EVENT_CHECKPOINT = "research.checkpoint"
EVENT_PLAN = "research.plan"
EVENT_STEP_START = "research.step.start"
EVENT_STEP_SQL = "research.step.sql"
"""步 SQL 就绪（计划自带或逐步 NL2SQL 生成后）。"""
EVENT_STEP_DATA = "research.step.data"
"""步数据就绪（只读查询返回行）。"""
EVENT_STEP_CHART = "research.step.chart"
"""步出图完成（图表类型 + option）。"""
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
ERROR_SQL_VALIDATION_FAILED = "sql_validation_failed"
"""SQL Guard 拒绝的 error code（与 SIGNAL_SQL_VALIDATION_FAILED 同值，但语义是 error code 侧）。"""
ERROR_STEP_FAILED = "step_failed"
"""通用步失败的 error code（与 checkpoint signal 词汇 `low_confidence_step` 解耦）。"""
LLM_UNAVAILABLE_MESSAGE = "无可用 LLM 客户端：本轮 LLM 段降级（计划/假设/报告文本可能不完整）"
DEFAULT_MODE = "research"


@dataclass(frozen=True)
class ErrorSpec:
    """一个 research.error code 的完整契约（终态判定 / 会话状态 / payload 附加字段 / 前端处置）。"""

    terminal: bool  # True ⇒ 事件后关流
    sessionStatus: str  # 事件发出后该会话的预期状态
    payloadFields: tuple[str, ...]  # 除 code / message / uiHint（恒在）外的逐 code 固定附加字段
    uiHint: str  # 前端处置（按「类」而非按 code 分支的依据）
    summary: str


# --- error code SSOT（Task 8.5 用户裁定；勿自行增删 code 或改变 terminal 归属）----
# 注：sql_validation_failed / step_failed 的 sessionStatus 是**默认（等待用户）**语义——
# 落动态 checkpoint ⇒ awaiting_user；autoConfirm 测试模式下可能直接跑到 done。不为它加分支逻辑。
ERROR_SPECS: dict[str, ErrorSpec] = {
    ERROR_TURN_FAILED: ErrorSpec(
        terminal=True,
        sessionStatus=STATUS_FAILED,
        payloadFields=("phase",),
        uiHint="terminal",
        summary="turn 终止：终态错误，会话落 failed 并关流",
    ),
    ERROR_LLM_UNAVAILABLE: ErrorSpec(
        terminal=False,
        sessionStatus=STATUS_RUNNING,
        payloadFields=(),
        uiHint="degraded",
        summary="无可用 LLM：LLM 段降级，状态机继续推进",
    ),
    ERROR_HYPOTHESIS_FAILED: ErrorSpec(
        terminal=False,
        sessionStatus=STATUS_RUNNING,
        payloadFields=(),
        uiHint="degraded",
        summary="假设生成失败：降级为空候选，状态机继续推进",
    ),
    ERROR_SQL_VALIDATION_FAILED: ErrorSpec(
        terminal=False,
        sessionStatus=STATUS_AWAITING,
        payloadFields=("stepIndex",),
        uiHint="degraded",
        summary="SQL Guard 拒绝：落动态 low_confidence_step（默认等待用户）",
    ),
    ERROR_STEP_FAILED: ErrorSpec(
        terminal=False,
        sessionStatus=STATUS_AWAITING,
        payloadFields=("stepIndex",),
        uiHint="degraded",
        summary="通用步失败：落动态 low_confidence_step（默认等待用户）",
    ),
}

TERMINAL_ERROR_CODES = frozenset(
    code for code, spec in ERROR_SPECS.items() if spec.terminal
)
"""终态错误码集合：**由 ERROR_SPECS 派生**，判定处不得自持字面量集合。"""


def errorPayload(code: str, message: str, **fields: Any) -> dict[str, Any]:
    """按 ERROR_SPECS 构造 research.error payload：code/message/uiHint（表派生）恒在，
    `**fields` 必须与 payloadFields（逐 code 声明的额外字段）完全一致，不符或未知 code 抛 ValueError。"""
    spec = ERROR_SPECS.get(code)
    if spec is None:
        raise ValueError(f"未知 error code: {code!r}")
    if set(fields) != set(spec.payloadFields):
        raise ValueError(
            f"error payload 字段集不符: code={code!r} 期望 {set(spec.payloadFields)} 实得 {set(fields)}"
        )
    return {"code": code, "message": message, "uiHint": spec.uiHint, **fields}


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
        cachedTokens: int | None = None,
    ) -> None: ...


class Reporter(Protocol):
    """ReportPlanner（Task 6）契约：`compose(...) -> (payload, rendered_md)`。

    只负责**装配**（payload + rendered_md）；归档（`publishReport`：version=max+1、
    旧 published → superseded）由调用方 `_stageReport` 完成 —— 故本协议的任何实现
    （真实 `ReportPlanner` / 测试 fake）都可互换。
    """

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
        cachedTokens: int | None = None,
    ) -> None:
        if promptTokens + completionTokens <= 0:
            return
        config = await self._findConfig(session, modelName)
        if config is None and modelName:
            logger.warning("计量未匹配到 llm_config，成本按 0 记: model=%s", modelName)
        multiplier = await self._cacheHitMultiplier(session, cachedTokens)
        cost = self._costFor(
            config,
            promptTokens,
            completionTokens,
            cachedTokens=cachedTokens,
            cacheHitMultiplier=multiplier,
        )
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
    async def _cacheHitMultiplier(session: AsyncSession, cachedTokens: int | None) -> float:
        """prompt-cache 折扣常数：现读 chat 同源 SSOT `system_config.LLM_CACHE_HIT_MULTIPLIER`。

        Task 6.5-3：研究链路此前完全不认缓存命中（既不计 cachedTokens 也不打折），
        与 chat 口径不一致。折扣常数**不自算**，一律现读同一行配置（默认 0.0 =
        命中免费，与初版差额计费等价）。未报命中时不查库（省一次查询）。
        """
        if not cachedTokens or cachedTokens <= 0:
            return 0.0
        # 延迟导入：避开模块导入期的重依赖（nl2sql_service 体量大且非本模块必需）。
        from app.services.nl2sql_service import _readFloatConfig

        try:
            return await _readFloatConfig(session, LLM_CACHE_HIT_MULTIPLIER_KEY, 0.0)
        except Exception:  # noqa: BLE001 —— 读配置失败按「命中免费」，不阻断记账
            logger.warning("读取 %s 失败，缓存命中按 0 计", LLM_CACHE_HIT_MULTIPLIER_KEY, exc_info=True)
            return 0.0

    @staticmethod
    async def _findConfig(session: AsyncSession, modelName: str | None) -> LlmConfig | None:
        if not modelName:
            return None
        return await session.scalar(
            select(LlmConfig).where(LlmConfig.model_name == modelName).limit(1)
        )

    @staticmethod
    def _costFor(
        config: LlmConfig | None,
        promptTokens: int,
        completionTokens: int,
        cachedTokens: int | None = None,
        cacheHitMultiplier: float = 0.0,
    ) -> Decimal:
        """成本 = 未命中 prompt 全额 + 命中 prompt × 折扣 + 输出全额。

        与 chat `UsageMixin._costFor` **逐字同口径**（等价 token 口径
        `billable = (prompt - cached) + cached × multiplier`），由单测对拍钉住。
        """
        if config is None:
            return Decimal("0")
        billablePrompt = promptTokens
        if cachedTokens is not None and cachedTokens > 0:
            billablePrompt = max(
                0, (promptTokens - cachedTokens) + cachedTokens * cacheHitMultiplier
            )
        return (
            Decimal(billablePrompt) * Decimal(str(config.cost_per_1k_input))
            + Decimal(completionTokens) * Decimal(str(config.cost_per_1k_output))
        ) / Decimal(1000)


class MeteredClient:
    """LLM 客户端包装：透传 `complete`，**累加**本轮全部调用的 token 与模型名。

    适配层 `generateHypotheses` 只回传假设列表（token 在响应里被丢掉），故计量必须在
    客户端边界捕获——否则「假设生成的 LLM 调用」成为计量盲区（核心约束 #3）。

    累加语义（Task 6）：报告阶段一次 `compose` 会逐块调用 LLM（执行摘要 + 每条结论的
    解读），只保留「最后一次」会漏计前面的调用；累加后调用方一次性记账。

    缓存口径（Task 6.5-3）：`cachedTokens` 同样累加；任一轮响应**没有**该字段则整体记 `None`
    （与 nl2sql 4-1 同口径：宁记「未知」也不谎报命中，否则把 miss 误折成 hit 少计费）。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.promptTokens = 0
        self.completionTokens = 0
        self.cachedTokens: int | None = 0
        self.modelName: str | None = None

    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        response = await self._inner.complete(messages, **kwargs)
        self.promptTokens += int(getattr(response, "promptTokens", 0) or 0)
        self.completionTokens += int(getattr(response, "completionTokens", 0) or 0)
        cached = getattr(response, "cachedTokens", None)
        if cached is None:
            self.cachedTokens = None
        elif self.cachedTokens is not None:
            self.cachedTokens += int(cached)
        self.modelName = getattr(response, "modelName", None) or self.modelName
        return response


# Task 5 的占位 `DefaultReporter` 已删除：Task 6 起 `ReportPlanner` 是唯一实现，其自身覆盖
# 原占位的两条兜底（`llmClient=None` → 模板文案；无 finding → 空数据段），保留即死代码。


# ---------------------------------------------------------------------------
# 无状态构件：相位映射与步错误码
# （options 构造 / 步 signal / 计划归一化等唯一消费者在 phase 执行体的构件，
#  见 research_agent_phases.py；恢复态重建见 research_agent_stages.py，Task 8 抽出）
# ---------------------------------------------------------------------------


def requireQuestion(question: str, sessionId: uuid.UUID | str) -> None:
    """问题守卫（SSOT）：空白问题一律 ValueError（`startTurn` / `runTurn` 共用）。

    Task 7.5 LOW：原只在 `startTurn`，API 路径 `createTurn → appendTurn → runTurn` 绕过了它；下沉后同源。
    """
    if not question or not question.strip():
        logger.warning("研究 turn 问题为空: session=%s", sessionId)
        raise ValueError("研究问题不能为空")


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


def stepErrorCode(error: str | None) -> str:
    """步失败 **error code**：SQL Guard 拒绝 → sql_validation_failed，其余 → step_failed。

    与 `research_agent_phases.stepSignal`（checkpoint signal 词汇，Task 9 fix round 1 搬到那里）
    解耦：error code 不进 phase / options["signal"] 白名单。
    """
    if error and error.startswith(SIGNAL_SQL_VALIDATION_FAILED):
        return ERROR_SQL_VALIDATION_FAILED
    return ERROR_STEP_FAILED


# ---------------------------------------------------------------------------
# 无状态构件：步结果归一化
# （单步回落 / 计划归一化 / 计划反馈回灌见 research_agent_phases.py，
#  Task 9 fix round 1 搬到那里 —— 唯一消费者是 phase 执行体）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 无状态构件：LLM 客户端兜底 / 用量记账兜底
# （Task 6 从 research_agent_service 抽出，保持该文件 ≤ 800 行）
# ---------------------------------------------------------------------------


def buildClient(factory: Any, config: Any) -> Any:
    """按注入 factory + **已选模型配置**取客户端；创建失败降级为「无 LLM」并留痕。

    `config is None` 表示「无可用模型配置」——此处**绝不回退 `factory(None)`**：
    那正是 keyless 静默降级的来源（Task 5 MEDIUM-4），也是 Task 6.5-2 要根除的
    `factory(None)` 口径。无配置时直接返回 None，由 `resolveClient` 走显式降级。
    """
    if factory is None or config is None:
        return None
    try:
        return factory(config)
    except Exception:  # noqa: BLE001 —— 客户端创建失败降级为「无 LLM」，不中断 turn
        logger.error("LLM 客户端创建失败，本次降级跳过 LLM 段", exc_info=True)
        return None


async def buildRoutingContext(
    session: AsyncSession | None, sessionId: uuid.UUID | str, tokenUsage: Any
) -> RoutingContext:
    """按 chat 同口径装配路由上下文（对拍 `chat_service._buildRoutingContext`）。

    M1（fix round 1）：此前只传 `sessionId`，`sessionCost` / `sessionTurnCount` /
    `priorModelId` 全取默认值 ⇒ 路由器的**预算超限降级**（`sessionCost >= sessionBudget`
    ⇒ 最便宜）与**会话亲和**（`turnCount < affinityTurns` 且 `priorModelId` 命中 ⇒ 沿用）
    两条规则对研究链路恒不触发，等于路由失效。三项读数一律走 `TokenUsageService` 的公开面
    （getSessionCost / getSessionTurnCount / getLastModelId），不自算、不读 chat 私有。
    """
    if tokenUsage is None or session is None:
        return RoutingContext(sessionId=str(sessionId))
    try:
        # 同 resolveModelConfig：读用量失败也必须 savepoint 隔离，否则主事务留在 aborted 态，后续写入全炸。
        async with session.begin_nested():
            cost = await tokenUsage.getSessionCost(session, str(sessionId))
            turns = await tokenUsage.getSessionTurnCount(session, str(sessionId))
            prior = await tokenUsage.getLastModelId(session, str(sessionId))
    except Exception:  # noqa: BLE001 —— 读用量失败按零上下文路由，不阻断整轮
        logger.warning("装配路由上下文失败，按零上下文路由: session=%s", sessionId, exc_info=True)
        return RoutingContext(sessionId=str(sessionId))
    return RoutingContext(
        sessionId=str(sessionId),
        sessionCost=float(cost or 0),
        sessionTurnCount=int(turns or 0),
        priorModelId=prior,
    )


class PreferredModelUnavailableError(RuntimeError):
    """会话显式选定的模型不可用（不存在 / 已停用 / key 缺失）。

    **绝不静默回落自动路由**（静默替换正是要修的「以为用了 A 实际用了 B」误判）。由
    `_guardedRun` 捕获 → 发 `research.error{turn_failed}`（终态）→ markFailed。
    """


async def resolveModelConfig(
    modelConfigs: Any,
    modelRouter: Any,
    factory: Any,
    session: AsyncSession | None,
    *,
    question: str,
    sessionId: uuid.UUID | str,
    tokenUsage: Any = None,
    preferredModelId: int | None = None,
) -> Any | None:
    """按 chat 同口径选出本轮模型配置：`list(activeOnly)` → 可用性筛 → 路由。

    与 chat `_buildPipelineContext` 同源（Step 0）：
    - 配置清单走公开面 `ModelConfigService.list(session, activeOnly=True)`；
    - 「可用」判据复用注入的 `factory`（真实实现即 `createClient`，key 解析的 SSOT），
      逐配置隔离异常（单条密文损坏不应让整轮路由失败）；
    - 选择走公开面 `ModelRouterService.selectModel(configs, prompt, ctx)`，其中 `ctx`
      由 `buildRoutingContext` 装配（预算 / 轮次 / 上次模型三项齐备）。

    `preferredModelId` 非空表示**会话级显式选定**（W5-b）：按**全量**清单直选、跳过
    router；不可用（不存在 / 已停用 / key 缺失）一律抛 `PreferredModelUnavailableError`，
    不回落。其余任何一步失败/为空都返回 None（调用方走 `research.error` 显式降级），
    不抛错、不静默用 keyless 客户端顶替。
    """
    if modelConfigs is None:
        return None
    if preferredModelId is not None:
        return await _resolvePreferredConfig(
            modelConfigs, factory, session, preferredModelId, sessionId=sessionId
        )
    configs = await _readConfigs(modelConfigs, session, sessionId, activeOnly=True)
    if configs is None:
        logger.warning("读取模型配置失败，本轮 LLM 段降级: session=%s", sessionId)
        return None
    usable = [config for config in configs if buildClient(factory, config) is not None]
    if not usable:
        logger.warning("无可用模型配置（key 缺失/未配置），本轮 LLM 段降级: session=%s", sessionId)
        return None
    if modelRouter is None:
        return usable[0]
    try:
        ctx = await buildRoutingContext(session, sessionId, tokenUsage)
        return modelRouter.selectModel(usable, question, ctx)
    except Exception:  # noqa: BLE001 —— 路由失败（如 NoAvailableModelError）同样降级
        logger.warning("模型路由失败，本轮 LLM 段降级: session=%s", sessionId, exc_info=True)
        return None


async def _readConfigs(
    modelConfigs: Any, session: AsyncSession | None, sessionId: uuid.UUID | str, *,
    activeOnly: bool,
) -> list[Any] | None:
    """读模型配置清单；读失败返回 None（异常在此留痕，调用方只决定降级 / 报错）。

    独立 savepoint（Task 7.5 HIGH）：`list()` 语句级失败（如 llm_config 结构漂移）会把主事务
    置 aborted ⇒ 后续 checkpoint / resume INSERT 全炸（已发 202，数据静默丢失）；savepoint 隔离之。
    """
    try:
        async with session.begin_nested():
            return list(await modelConfigs.list(session, activeOnly=activeOnly))
    except Exception:  # noqa: BLE001 —— 读取失败不在此处置：调用方决定降级或报错
        logger.warning(
            "模型配置读取异常: session=%s activeOnly=%s", sessionId, activeOnly, exc_info=True
        )
        return None


async def _resolvePreferredConfig(
    modelConfigs: Any, factory: Any, session: AsyncSession | None, preferredModelId: int,
    *, sessionId: uuid.UUID | str,
) -> Any:
    """会话显式选定模型的**直选**：读**全量**清单、跳过 router；不可用即显式报错。

    三种不可用（不存在 / 已停用 / key 缺失）统一抛 `PreferredModelUnavailableError`，
    **绝不静默回落自动路由** —— 静默替换正是要修的「以为用了 A 实际用了 B」误判。
    """
    configs = await _readConfigs(modelConfigs, session, sessionId, activeOnly=False)
    if configs is None:
        logger.warning("读取模型配置失败（会话已指定模型）: session=%s", sessionId)
        raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
    target = next((config for config in configs if config.id == preferredModelId), None)
    if target is None or not target.is_active:
        raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
    if buildClient(factory, target) is None:
        # key 缺失 / 客户端建不起来：同样显式报错，不换模型。
        raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
    return target


async def markLlmUnavailable(
    state: dict[str, Any], *, emit: Emit | None, sessionId: uuid.UUID | str
) -> None:
    """无可用 LLM 的**显式**降级：state 标记 + warning + `research.error`（不静默 done）。"""
    state["llmUnavailable"] = True
    logger.warning("无可用 LLM 客户端，本轮 LLM 段降级: session=%s", sessionId)
    await emitEvent(
        emit,
        EVENT_ERROR,
        errorPayload(ERROR_LLM_UNAVAILABLE, LLM_UNAVAILABLE_MESSAGE),
    )


async def resolveClient(
    factory: Any,
    modelConfigs: Any,
    modelRouter: Any,
    session: AsyncSession | None,
    *,
    state: dict[str, Any],
    emit: Emit | None,
    sessionId: uuid.UUID,
    tokenUsage: Any = None,
    preferredModelId: int | None = None,
) -> tuple[Any, Any]:
    """解析本轮 LLM 客户端与其模型配置；无可用时显式降级并返回 `(None, None)`。

    `preferredModelId`（显式选定）透传直选；不可用时抛 `PreferredModelUnavailableError`。
    """
    config = await resolveModelConfig(
        modelConfigs,
        modelRouter,
        factory,
        session,
        question=str(state.get("question") or ""),
        sessionId=sessionId,
        tokenUsage=tokenUsage,
        preferredModelId=preferredModelId,
    )
    client = buildClient(factory, config)
    if client is None:
        await markLlmUnavailable(state, emit=emit, sessionId=sessionId)
        return None, None
    return client, config


async def recordUsageQuietly(
    recorder: UsageRecorder,
    session: AsyncSession,
    *,
    sessionId: uuid.UUID,
    purpose: str,
    promptTokens: int,
    completionTokens: int,
    modelName: str | None,
    cachedTokens: int | None = None,
) -> None:
    """记一次 LLM 用量；记账失败只留痕（不能把已发生的消耗变成未知）。

    零消耗直接跳过（模板降级 / 未真正调 LLM 的相位不写空台账行）。
    """
    if promptTokens + completionTokens <= 0:
        return
    try:
        await recorder.recordUsage(
            session,
            sessionId=str(sessionId),
            purpose=purpose,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            modelName=modelName,
            cachedTokens=cachedTokens,
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


# ---------------------------------------------------------------------------
# 无状态构件：步本体类筛选（Task 6.5；计划反馈回灌见 research_agent_phases.py）
# ---------------------------------------------------------------------------


def selectClassesForTables(classes: list[Any], tables: list[str]) -> list[Any]:
    """按 ESL 三臂给出的物理表名筛本体类（大小写不敏感）；无匹配时退回全量。

    退回全量而非空表：`generateSql` 的 schema 小节需要上下文，空类表会让生成必然
    失败，把「本来可答」的问题变成 `STEP_MISSING_SQL`；全量只是贵一点。
    """
    wanted = {str(table).strip().lower() for table in tables if str(table).strip()}
    if not wanted:
        return list(classes)
    matched = [
        cls
        for cls in classes
        if str(getattr(cls, "source_table", "") or "").strip().lower() in wanted
    ]
    return matched or list(classes)
