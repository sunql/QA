"""ChatService 共享纯函数与数据类（从 chat_service 拆出）。

这些是「被 2+ 个 mixin / 基类共用」的模块级符号：纯文本裁剪、历史计划解析、
步骤错误文案、重试留痕、断连兜底状态、流水线上下文数据类。它们**不读 `self`**，
故可安全抽出；各 mixin 与基类按需 `from app.services.chat_helpers import ...`。

⚠️ 这里放「跨职责共享」的符号；单一职责的私有函数应随其 mixin 走（如类召回
辅助进 `chat_recall.py`），不要把所有顶级函数都堆进来，避免再长成第二个大文件。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, LlmConfig, SessionQueryState
from app.domain.multi_step_plan import StepPlan, StepResult
from app.domain.plan_drop import formatPlanDrops
from app.domain.query_plan import QueryPlan
from app.domain.schemas import ClassRecallInfo
from app.infrastructure.business_db_pool import BusinessDbAdapter
from app.infrastructure.llm.base_client import BaseLlmClient
from app.services.audit_service import AuditService
from app.services.messages_zh import MSG_SPEAKER_ASSISTANT, MSG_SPEAKER_USER

logger = logging.getLogger(__name__)

# M3：历史查询计划（session_query_state.last_plan JSONB）解析出现内容级丢弃时的
# 日志 reason。历史可能来自旧版本或被直写破坏 —— 只上报，**不收紧**解析口径：
# 收紧会让历史会话整段失败（_buildStatePrompt 拿不到计划、REFINE 直写被误判失败）。
_REASON_PLAN_HISTORY_DEGRADED = "PLAN_HISTORY_DEGRADED"


# 复合问题启发式（2026-08-17 真实回归）：用列举连接词连接 ≥2 个查询动词的问题
# 应当被识别为多步（如"查询 A、查询 B、查询 C"），即使无显式分步信号。
# 严格守约：必须 ≥2 段、每段含至少 1 个查询动词、且不含禁用短语——
# 宁可让 LLM 多调一次（heuristic miss），不让单条查询被误拆为多步（heuristic hit）。
_COMPOUND_SEPARATOR_RE = re.compile(r"[，,；;、]|\s+和\s+|\s+以及\s+|\s+及\s+|\s*\+\s*")
# 查询动词：仅这些动词触发起步检查，避免"按金额降序""按月度分组"等修饰语误触。
_QUERY_VERBS_RE = re.compile(r"统计|查询|列出|计算|对比|分析|排序|获取|筛选|展示|求|算|找|看|查")
# 禁用短语：用户明确不要拆分时跳过启发式（让单条 SQL 路径处理）。
_COMPOUND_DENY_PHRASES = ("不要拆", "不要分", "用一条", "单条查询", "一条 SQL")


def _looks_like_compound_question(question: str) -> bool:
    """识别并列复合问题（无显式分步信号但语义多步）。

    两种触发模式（任一命中即视为复合）：
    1. **多动词并列**：≥2 段都含查询动词（"查询 A + 查询 B + 查询 C"）。
    2. **头+列表**：≥3 段且首段含查询动词（"查询 A、B、C" — 动词隐式作用于所有尾段，
       如用户真实场景"查询3月份采购订单数量、Top 10物料占比、Top 10物料在4月份的订单数量"）。

    反例守约：
    - "对比 A 和 B" → 2 段、1 动词 → 不触发（避免误拆"对比"型单步查询）
    - "查询 A，按金额降序" → 2 段、1 动词 → 不触发（"按…排序"是修饰）
    - "查询 A 和 B" → 2 段、1 动词 → 不触发（保守：宁可让 LLM 多调一次）
    """
    q = question.strip()
    if not q:
        return False
    if any(p in q for p in _COMPOUND_DENY_PHRASES):
        return False
    parts = [p.strip() for p in _COMPOUND_SEPARATOR_RE.split(q) if p.strip()]
    if len(parts) < 2:
        return False
    verb_count = sum(1 for p in parts if _QUERY_VERBS_RE.search(p))
    # 模式 1：多动词并列
    if verb_count >= 2:
        return True
    # 模式 2：头+列表（≥3 段且首段有动词）
    if verb_count >= 1 and len(parts) >= 3:
        return True
    return False


def _speakerFor(role: str) -> str:
    """消息角色 → 中文说话人标签。"""
    return MSG_SPEAKER_USER if role == "user" else MSG_SPEAKER_ASSISTANT if role == "assistant" else role


def _statePlan(state: SessionQueryState) -> QueryPlan | None:
    """解析上一轮查询计划；last_plan 缺失返回 None（对损坏输入全容错）。

    M3：内容级丢弃（字段类型损坏等）单点记一条 reason= 日志。**不收紧**口径——
    返回空计划而非 None：调用点（REFINE 直写闸门 `plan is None`）以 None 判成败，
    收紧会把成功的重写判死；历史 JSONB 也可能来自旧版本。
    """
    if not state.last_plan:
        return None
    plan, drops = QueryPlan.from_dictWithReport(state.last_plan)
    if drops:
        logger.warning(
            "历史查询计划解析降级 reason=%s drops=%s",
            _REASON_PLAN_HISTORY_DEGRADED,
            formatPlanDrops(drops),
        )
    return plan


def _snapshotRound(state: SessionQueryState) -> dict[str, Any]:
    """从当前 last_* 字段构造一个历史快照（question + sql），压入 recent_rounds 前。"""
    return {"q": state.last_question, "s": state.last_sql}


def _step_result_to_read(result: StepResult) -> "StepResultRead":
    """将 StepResult 转换为 API 响应的 DTO（延迟导入避免循环）。"""
    from app.domain.schemas import StepResultRead
    return StepResultRead(
        step_index=result.step_index,
        description=result.description,
        sub_question=result.sub_question,
        sql=result.sql,
        data=result.data if result.data else None,
        summary=result.summary,
        error=result.error,
    )


def _logEmbeddingTaskFailure(task: asyncio.Task[Any]) -> None:
    """记录后台查询向量存储任务的未预期异常（预期失败已在服务内部记录日志）。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("后台查询向量存储任务异常: %s", exc, exc_info=exc)


def _clipText(text: str, limit: int) -> str:
    """截断文本到指定字符上限，超出时追加省略号；返回新字符串，不改动入参。"""
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _fitPartsToBudget(parts: list[str], budget: int) -> list[str]:
    """按预算从最新往回保留整块历史，返回新列表（不改动入参）。

    入参按「旧 → 新」排列。超预算时丢最旧的**整块**而不是截掉最新内容：最新一轮是
    追问（REFINE / FOLLOW_UP）的锚点，宁可少几轮旧上下文。至少保留最新一块 —— 预算
    被误调得过小时表现为「只剩最新一轮」，而不是清空历史；此时再把最新一块（kept[-1]）
    裁进预算，保证返回文本总长恒有界（预算 + 省略号）。
    """
    kept: list[str] = []
    used = 0
    for part in reversed(parts):
        extra = len(part) + (1 if kept else 0)  # "\n" 分隔符
        if kept and used + extra > budget:
            break
        kept.append(part)
        used += extra
    kept.reverse()
    # 循环保证「多块留存时每块都在预算内」，故此处只可能对唯一留存的最新块生效
    if kept and len(kept[-1]) > budget:
        kept[-1] = _clipText(kept[-1], budget)
    return kept


def _summarizeExecutionError(exc: Exception) -> str:
    """从执行异常提取简短错误信息，回灌给 LLM 修正 SQL（1-3）。

    对于 Nl2SqlError，具体校验失败原因在 detail（如"选中的类 X 不在本体"），
    message 是通用提示。优先取 detail 以提供具体上下文。
    """
    if hasattr(exc, "detail") and exc.detail:
        return exc.detail
    return getattr(exc, "message", None) or str(exc)


# 多步失败隔离（C3）：步骤级错误文案前缀。两类分开，便于日志与前端区分
# 「根本没生成出 SQL」与「生成了但执行失败（含回灌重试）」。
_STEP_GEN_FAILED_PREFIX = "该步骤查询生成失败："
_STEP_EXEC_FAILED_PREFIX = "该步骤执行失败："
_STEP_FAILED_ERROR_LIMIT = 200  # 步骤错误文案字符上限（避免把整段堆栈塞进响应）
# 两段（首次 / 重试）各自的上限：只做整体尾部截断的话，一段超长的首次原因会把
# 「重试为什么也没救回来」整段挤掉 —— 那恰恰是 M7 要暴露的信息（实测 500 字首次原因
# 下重试原因完全消失）。两段各自限量后，两段之和仍受 _STEP_FAILED_ERROR_LIMIT 约束。
_STEP_FAILED_SEGMENT_LIMIT = 90
# 软失败（LLM 判定无有效查询计划）：非硬异常，纯步骤级隔离
_MSG_STEP_UNANSWERABLE = "无法回答（LLM 判定无有效查询计划）"
# 汇总步骤被跳过（前置数据步骤全失败）：非失败、非成功，如实说「未执行」
_MSG_STEP_AGGREGATION_SKIPPED = "未执行（前置数据步骤全部失败）"
# SQLAlchemy 语句异常的 `str()` 会在驱动原因之后追加这两段。它们**只**留给服务端日志与
# 回灌 LLM 的重试反馈（`_summarizeExecutionError`），进用户可见文案会泄漏内部表/列名
# （SQL 全文）与查询字面量（参数可能含业务数据）。
_SQL_DETAIL_MARKERS = ("[SQL:", "[parameters:")
_MSG_STEP_ERROR_FALLBACK = "执行失败（详见服务端日志）"


def _userFacingErrorText(exc: Exception) -> str:
    """用户可见的错误原因：只保留驱动给的首段，剥掉 SQL 全文与参数细节。"""
    text = _summarizeExecutionError(exc)
    for marker in _SQL_DETAIL_MARKERS:
        index = text.find(marker)
        if index != -1:
            text = text[:index]
    return text.strip() or _MSG_STEP_ERROR_FALLBACK


def _stepFailedError(exc: Exception, prefix: str) -> str:
    """把步骤级硬异常收敛为可展示的步骤错误文案（截断，不含堆栈）。

    M7：回灌重试也失败时，二次失败原因一并展示（「首次：… ；重试…：…」）。此前
    只报首次错误，用户看到「这一步的 SQL 一开始就是错的」，而真相是「首次错了、
    回灌重试同样错」—— 两句话对应的排查方向完全不同。两段各自走
    `_userFacingErrorText`（SQL 全文与参数字面量一律剥掉，且各自有兜底文案），
    并**各自**限量（`_STEP_FAILED_SEGMENT_LIMIT`）后拼接，最后再套总上限。
    """
    text = _userFacingErrorText(exc)
    retryFailure = _retryFailure(exc)
    if retryFailure is not None:
        retryText = _userFacingErrorText(retryFailure.error)
        text = (
            f"首次：{_clipText(text, _STEP_FAILED_SEGMENT_LIMIT)}；"
            f"重试{retryFailure.stageLabel}："
            f"{_clipText(retryText, _STEP_FAILED_SEGMENT_LIMIT)}"
        )
    return prefix + _clipText(text, _STEP_FAILED_ERROR_LIMIT)


# M7：回灌重试的**二次失败**详情。此前只进一行服务端日志，用户可见的步骤文案只报
# 首次错误 —— 「重试为什么也没救回来」在对外视野里彻底消失（看起来像 SQL 一上来就
# 写错了，排查方向完全不同）。与 `_RETRY_GEN_TOKENS_ATTR` 同思路：挂私有属性，
# 不改异常类型与消息（API 层按类型映射 HTTP 状态与错误码）。
_RETRY_FAILURE_ATTR = "_retryFailure"


@dataclass(frozen=True)
class _RetryFailure:
    """回灌重试二次失败的原地留痕。

    `stageLabel` 会直接拼进用户可见文案（「… ；重试{stageLabel}：…」），因此是
    面向用户的中文短语，不是内部枚举名。
    """

    stageLabel: str
    error: Exception


def _attachRetryFailure(exc: Exception, stageLabel: str, error: Exception) -> None:
    """把二次失败详情挂到上抛的首次异常上（只挂私有属性，不改类型/消息）。"""
    setattr(exc, _RETRY_FAILURE_ATTR, _RetryFailure(stageLabel=stageLabel, error=error))


def _retryFailure(exc: Exception) -> _RetryFailure | None:
    """提取异常携带的二次失败详情；未携带时返回 None。"""
    return getattr(exc, _RETRY_FAILURE_ATTR, None)


# 重试 SQL 写进服务端日志时的字符上限（自诊断用）：重试执行仍失败时，这条 SQL 是
# 「为什么重试也没救回来」唯一的证据，但它此前没被记在任何地方。截断只为避免超长
# SQL 刷屏；服务端日志本就含执行错误的 `[parameters: ...]` 细节，不改变既有边界。
_RETRY_SQL_LOG_LIMIT = 2000


@dataclass(frozen=True)
class _RetryGenUsage:
    """回灌重试**生成**阶段已消耗的用量增量（供调用方累加进总额与台账）。"""

    tokens: int
    cost: Decimal
    modelName: str


def _failedStepResult(step_plan: StepPlan, error: str) -> StepResult:
    """构造失败步骤的 StepResult。

    sql 一律 None：与 `_finalizeMultiStepDegrade` / `_hasDataStepResult` 的
    「sql 非 None 即成功」判据同口径，同时让「无数据」在下游 prompt 里可识别。
    """
    return StepResult(
        step_index=step_plan.index,
        description=step_plan.description,
        sub_question=step_plan.sub_question,
        sql=None,
        error=error,
    )


def _hasDataStepResult(completed: list[StepResult]) -> bool:
    """截止当前（调用时点）已完成的数据步骤里，是否有产出过结果的。

    判据与 `_finalizeMultiStepDegrade` 一致：`sql is not None` 即成功
    （失败步骤一律 sql=None），保证两处对「成功」的定义不会漂移。

    注意语义是「已完成的步骤里有没有成功」而非「计划里所有数据步骤都失败了」：
    调用点在汇总步分支，正常计划（汇总步在末尾，`StepQueryPlanner` 两处构造点均如此）
    下两者等价；若将来出现「汇总步在末尾之前」的畸形计划，本函数会因后续数据步尚未
    执行而判为 False，此时 `continue` 只是跳过汇总、后续数据步照常执行并在循环后
    走降级收尾——不会编造结论，但那段结果不会被汇总。真要支持该形态需在循环前对
    `data_steps` 全集判断。
    """
    return any(r.sql is not None for r in completed)


LlmFactory = Callable[[Any], BaseLlmClient]
AdapterProvider = Callable[[int, DataSource], BusinessDbAdapter]
FallbackCaller = Callable[[LlmConfig], Awaitable[Any]]

_audit = AuditService()


@dataclass(frozen=True)
class _PipelineContext:
    """一轮查询流水线的共享上下文（数据源/本体/模型/历史，各加载一次）。"""

    ds: DataSource
    classes: list[Any]
    configs: list[LlmConfig]
    selected: LlmConfig
    client: BaseLlmClient
    contextPrompt: str
    # 1-2：历史相似成功 SQL 的 few-shot 示例文本；检索不可用/无相似命中时为 None
    fewShot: str | None = None
    # 2-1：关键列值域采样 {(source_table, source_column): [去重值]}；采样失败/无候选时为空
    valueSamples: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # 2-4：本体表/列漂移告警文本（buildDriftWarning 渲染）；无漂移/未缓存/CLARIFY 时为 None
    driftWarning: str | None = None
    # B3：术语词典文本（buildDictionaryText 渲染）；空表/加载失败时为 None
    dictionaryText: str | None = None
    # Phase 4.4：可用 Feature 目录文本（buildFeatureCatalogText 渲染）；
    # 空目录/加载失败时为 None（增强非依赖，行为与之前一致）
    featureCatalogText: str | None = None
    # 关联关系目录（OntologyJoin 边列表，运行时 JOIN 唯一真源）；空目录时为 []
    joins: list[Any] = field(default_factory=list)
    # 用户是否明确选择了模型（而非自动路由）；明确时跳过模型降级
    forcedModel: bool = False
    # 类召回诊断（2026-09-16）：截断/降级透出到响应，前端据此提示
    recall: ClassRecallInfo | None = None


@dataclass(frozen=True)
class _SqlOutcome:
    """ReAct 两阶段（或 REFINE 捷径）的结果：SQL + 计划 + 用量。

    sqlConfig 为 None 表示 REFINE 捷径命中（零 LLM 消耗，未记录 nl2sql usage）；
    sql 为 None 表示计划 target=无法回答（未生成 SQL，由调用方短路为友好回答）。
    """

    plan: QueryPlan | None
    sql: str | None
    sqlConfig: LlmConfig | None
    promptTokens: int = 0
    completionTokens: int = 0
    wasted: tuple[int, int] = (0, 0)
    # 4-1（feat-token-cache）：DeepSeek prompt cache 命中 token 数。None =
    # 未读/不支持。_costForSql 用它按差额计费（命中部分不计 input）。
    cachedTokens: int | None = None


@dataclass(frozen=True)
class _StepRun:
    """一个数据步骤的执行产物（含失败隔离后的错误行）。

    result：步骤结果；失败时是 error 行（sql=None、data=[]），成功时含 SQL + 数据。
    tokens/cost：该步骤消耗的 token / 成本（含值域采样浪费与回灌重试那一次），
        无论成功失败都已实际花掉，由调用方计入总量与审计行（核心约束 #3）。
    modelName：该步骤实际服务的模型名（供响应的 modelName 展示）。
    plan：仅成功步骤有值；调用方据此更新「最后一个成功步骤」的追问锚点。
    """

    result: StepResult
    tokens: int = 0
    cost: Decimal = Decimal("0")
    modelName: str | None = None
    plan: QueryPlan | None = None


# 断连兜底状态在 session.info 上的槽位键（H4）。用会话自身当载体，是因为
# 「生成器」与「响应收尾的 background 任务」必须看到**同一个**可变对象 ——
# 走形参就得给 processMessageStream/_streamQuery/_streamMultiStep 全加一遍签名。
_STREAM_PERSIST_KEY = "_streamPersistState"


@dataclass
class StreamPersistState:
    """一轮流式请求「已下发给客户端但尚未落库」的产出快照（H4 断连兜底）。

    生命周期（单发标志，保证只写一次）：
    - `pending`：`processMessageStream` 一进入就置 True（此后任何 yield 都可能已被
      客户端看到）；**任何一次成功的 `_storeSessionMessages` 都会置回 False** ——
      落库点即解除点，所以「哪些路径先落库后 yield」不需要逐个记住；
    - 断连时生成器多半停在 `yield` 上（不在任务栈上，`finally` 不触发），`pending`
      仍为 True ⇒ `StreamingResponse(background=...)` 用本快照补写。

    例外说明（不可变规则）：本类是**每请求可变持有器**，字段就地赋值是刻意的 ——
    它必须按引用与 background 任务共享，返回新副本就失去了意义。字段写全是廉价
    赋值（追加字符串 / 赋标量），不涉及 IO。
    """

    pending: bool = False
    # 已下发给客户端的回答片段（与 _streamQuery 的 answerPieces 同一个列表对象）
    answerPieces: list[str] = field(default_factory=list)
    sql: str | None = None
    plan: QueryPlan | None = None
    resultColumns: list[str] = field(default_factory=list)
    # 已**测得**的成本（计划/SQL/图表/已完成的调用）；断连时答复 token 还没到，
    # 故这是下界，不是真实成本 —— 兜底行的 token_cost_usd 沿用此值
    totalCostUsd: float = 0.0
    # 本轮起点（monotonic），兜底行 latency_ms 与正常落库同口径
    startedAt: float = 0.0


def attachStreamPersistState(session: AsyncSession) -> StreamPersistState:
    """取（必要时新建）本请求的断连兜底状态，挂在请求作用域 session 上。

    session 与 background 任务同寿命（FastAPI 的依赖 teardown 在响应体发完之后），
    故请求内是同一个实例、同一个 `info` 字典。
    """
    state = session.info.get(_STREAM_PERSIST_KEY)
    if state is None:
        state = StreamPersistState()
        session.info[_STREAM_PERSIST_KEY] = state
    return state


def streamPersistStateOf(session: AsyncSession) -> StreamPersistState | None:
    """读取本请求的断连兜底状态；非流式请求（未 attach）返回 None。"""
    return session.info.get(_STREAM_PERSIST_KEY)
