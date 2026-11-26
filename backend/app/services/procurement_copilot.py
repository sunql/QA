"""I3-a 采购 Copilot 统一对话入口（设计稿 §8.4）。

**定位**：Copilot 不新造执行引擎。它把「一句采购问题」翻译成一次
**采购域 Agent 的只读编排运行**（`POST /agent-runs` 同款链路），然后把
这次运行的**过程**以 SSE 推给前端，或把**任务引用**返回给调用方。

```
POST /procurement/copilot/messages
  ├─ agentCode 必须是 PROCUREMENT_ 前缀      （否则 422 —— 别把全局 Agent 混进采购面）
  ├─ AgentOrchestrationRunService.createRun  （全套门槛原样复用：ACTIVE / profile /
  │                                           model_config / 工具快照 / 上限 / 语义钉住）
  ├─ mode=task   → 202 {runId, status, stepsUrl}
  └─ mode=stream → SSE：run → step* → done/error
```

**为什么 SSE 只做「轮询转发」**：执行由独立 Worker 拥有（lease/续租/恢复都在
那边）。Copilot 若自己执行，就绕过了 claim/lease 与预算计量，等于复制一份
runner —— 这正是设计稿 §5.1「复用 Agent Runner」要避免的。

**会话隔离**：流内每次轮询用 `ownSession()` 自开会话，不复用请求会话
（请求会话在流式响应期间仍持有事务；见 `database.ownSession` 的二次
MissingGreenlet 事故说明）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_schemas import (
    AgentRunCreate,
    AgentRunCreated,
    AgentRunResume,
    AgentRunStepRead,
)
from app.domain.exceptions import ValidationError
from app.domain.procurement_copilot_schemas import (
    PROCUREMENT_AGENT_PREFIX,
    CopilotMessageRequest,
)
from app.infrastructure.database import getSessionFactory
from app.services import procurement_copilot_conversation as conversationStore
from app.services.agent_orchestration_run_service import (
    AgentOrchestrationRunService,
)
from app.services.agent_orchestration_types import PauseCode, RunStatus
from app.services.messages_zh import (
    MSG_COPILOT_NOT_PROCUREMENT_AGENT,
    MSG_COPILOT_STREAM_TIMEOUT,
)
from app.services.stream_events import StreamEvent

#: SSE 事件名（前端按此分派；不复用聊天 `token` 语义——Copilot 不做逐字流）。
EVENT_COPILOT_RUN = "copilot_run"
EVENT_COPILOT_STEP = "copilot_step"
EVENT_COPILOT_DONE = "copilot_done"
EVENT_COPILOT_ERROR = "copilot_error"

#: 轮询节奏与上限。`maxWaitSeconds` 只限制**一次 SSE 连接**的时长，
#: 不影响运行本身（超时后调用方可用 stepsUrl 继续跟进）。
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_WAIT_SECONDS = 120.0
MAX_STEPS_PER_POLL = 200

#: 终态（PAUSED 也算「本轮结束」：要么恢复，要么作废）。
TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.PAUSED,
    }
)


@dataclass(frozen=True)
class CopilotStreamOptions:
    """一次 SSE 连接的转发参数（可经 FastAPI 依赖覆盖：测试/运维调参）。"""

    pollInterval: float = DEFAULT_POLL_INTERVAL_SECONDS
    maxWaitSeconds: float = DEFAULT_MAX_WAIT_SECONDS


def copilotStreamOptions() -> CopilotStreamOptions:
    """默认转发参数（API 依赖注入点）。"""
    return CopilotStreamOptions()


StepsUrlBuilder = Callable[[str], str]

DEFAULT_STEPS_URL_PREFIX = "/api/v1/agent-runs"


def buildStepsUrl(runId: str) -> str:
    """运行轨迹地址（与 `GET /agent-runs/{runId}/steps` 同一路径）。"""
    return f"{DEFAULT_STEPS_URL_PREFIX}/{runId}/steps"


def assertProcurementAgent(agentCode: str) -> None:
    """Copilot 只接采购域 Agent（目录口径 `PROCUREMENT_` 前缀，同源）。"""
    if not agentCode.startswith(PROCUREMENT_AGENT_PREFIX):
        raise ValidationError(MSG_COPILOT_NOT_PROCUREMENT_AGENT.format(code=agentCode))


def toRunCreate(dto: CopilotMessageRequest, *, inputText: str | None = None) -> AgentRunCreate:
    """Copilot 请求 → 编排运行请求。

    `inputText` 只在多轮时覆盖（= 上下文块 + 当前问题）；其余字段**一对一**。
    """
    return AgentRunCreate(
        agent_code=dto.agent_code,
        input=inputText if inputText is not None else dto.message,
        model_config_id=dto.model_config_id,
        limits=dto.limits,
    )


async def createCopilotRun(
    session: AsyncSession,
    dto: CopilotMessageRequest,
    actor: Any,
    *,
    service: AgentOrchestrationRunService | None = None,
    inputText: str | None = None,
) -> AgentRunCreated:
    """校验域前缀 → 复用 `createRun` 落一个 QUEUED 运行。"""
    assertProcurementAgent(dto.agent_code)
    runner = service or AgentOrchestrationRunService()
    return await runner.createRun(session, toRunCreate(dto, inputText=inputText), actor)


@dataclass(frozen=True)
class CopilotTurnResult:
    """一轮的结果：跑在哪个 run 上、属于哪个会话、是否续跑。"""

    runId: str
    conversationId: str
    status: str
    resumed: bool


async def _resumeTargetRunId(
    session: AsyncSession,
    turns: list[Any],
    actor: Any,
    service: AgentOrchestrationRunService,
) -> str | None:
    """上一轮停在「等我回答」⇒ 返回其 runId（续跑）；否则 None（新开 run）。"""
    lastRunId = conversationStore.lastRunId(turns)
    if lastRunId is None:
        return None
    try:
        run = await service.getRun(session, lastRunId, actor)
    except Exception:  # noqa: BLE001 —— run 不可读（被删/异常）⇒ 当作无历史
        return None
    if run.status != RunStatus.PAUSED:
        return None
    if run.pause_code == PauseCode.NEEDS_INPUT:
        return lastRunId
    return None


async def _history(
    session: AsyncSession,
    turns: list[Any],
    actor: Any,
    service: AgentOrchestrationRunService,
) -> list[Any]:
    """最近几轮 → 上下文历史（逐 run 取答案；读不到就当作无答案）。"""
    views: dict[str, tuple[str | None, str]] = {}
    for turn in turns[-conversationStore.MAX_CONTEXT_TURNS :]:
        try:
            run = await service.getRun(session, turn.run_id, actor)
        except Exception:  # noqa: BLE001
            continue
        views[turn.run_id] = (
            run.final_answer,
            str(getattr(run.status, "value", run.status)),
        )
    return conversationStore.historyFromTurns(turns, views)


async def _sameDayReusableRunId(
    session: AsyncSession,
    *,
    dto: Any,
    actor: Any,
    question: str,
    dataStamp: str = "",
) -> str | None:
    """同日同问复用（2026-11-24 需求：一天内多次查询数据未变化 ⇒ 复用）。

    命中条件：同用户 + 同 Agent + 同模型 + **同问题文本** + **同数据新鲜度戳**
    + 当日 + 状态 ∈ {QUEUED, RUNNING, SUCCEEDED}（进行中的复用 = 相同问题并发
    去重；FAILED/CANCELLED **不复用** —— 给用户重试的机会）。
    数据戳（0171）：槽位实体在 feature_value 的最新 computed_at——指标重算
    ⇒ 戳变化 ⇒ 相同问题文本**不**命中旧 run（2026-11-25 需求：数据变了就重算）。
    复用 = 本轮 turn 直接指向该 run（SSE 跟随其终态，**零 LLM 成本**）；
    文字/参数/数据不同 ⇒ 正常新跑。resume（追问续跑）优先级更高，在上文已处理。
    """
    # 惰性导入：本模块被 run service 侧引用 ⇒ 顶层 import 模型会成环
    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    from app.domain.agent_orchestration_models import AgentOrchestrationRun
    from app.domain.procurement_copilot_models import ProcurementCopilotTurn

    todayStart = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    stmt = (
        sa_select(AgentOrchestrationRun.run_id)
        .join(
            ProcurementCopilotTurn,
            ProcurementCopilotTurn.run_id == AgentOrchestrationRun.run_id,
        )
        .where(
            AgentOrchestrationRun.created_by == str(getattr(actor, "userId", "")),
            AgentOrchestrationRun.agent_code == dto.agent_code,
            AgentOrchestrationRun.model_config_id == dto.model_config_id,
            AgentOrchestrationRun.status.in_(
                (RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.SUCCEEDED.value)
            ),
            AgentOrchestrationRun.created_time >= todayStart,
            ProcurementCopilotTurn.question == question,
            # NULL（非场景分支/旧行）与 ""（场景分支无特征数据）同视为无戳
            sa_func.coalesce(ProcurementCopilotTurn.data_stamp, "") == dataStamp,
        )
        .order_by(AgentOrchestrationRun.created_time.desc(), AgentOrchestrationRun.id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    return row


async def _scenarioDataStamp(session: AsyncSession, dto: CopilotMessageRequest) -> str:
    """场景分支的数据新鲜度戳（0171）：槽位里出现的实体键在 feature_value
    中的最新 computed_at（ISO 串）。

    - 非场景分支 / 无可解析实体 ⇒ ""（与旧行 NULL 同一复用桶，行为不变）；
    - 实体键从槽位值递归收集（标量/数组）——编码即实体标识（B125/A013 等）；
    - 指标重算 ⇒ computed_at 变化 ⇒ 戳变化 ⇒ 复用失效（数据变了就重算）。
    """
    if not dto.scenario_code or not dto.slots:
        return ""
    codes: set[str] = set()

    def _collect(value: Any) -> None:
        if isinstance(value, str):
            if value.strip():
                codes.add(value.strip())
        elif isinstance(value, (int, float)):
            codes.add(str(value))
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    for value in dto.slots.values():
        _collect(value)
    if not codes:
        return ""

    from sqlalchemy import func as sa_func
    from sqlalchemy import select as sa_select

    from app.domain.models import FeatureValue

    row = await session.execute(
        sa_select(sa_func.max(FeatureValue.computed_at)).where(FeatureValue.entity_key.in_(codes))
    )
    stamp = row.scalar()
    return stamp.isoformat() if stamp else ""


async def _renderScenarioQuestion(session: AsyncSession, dto: CopilotMessageRequest) -> str:
    """场景分支渲染（P4）：scenarioCode + slots 齐备才渲染，否则原样返回。

    - 场景必须存在且启用（getByCode fail loud）；
    - Agent 钉死校验：请求 agentCode 与场景配置不一致 ⇒ ValidationError
      （防止前端配置漂移后问句进了错误 Agent 的编排）；
    - 惰性导入：copilot_scenario 服务/渲染器顶层引 domain.models，
      本模块被 run service 侧引用，顶层 import 会成环。
    """
    if not dto.scenario_code or not dto.slots:
        return dto.message
    from app.services.copilot_scenario_render import renderScenarioQuestion
    from app.services.copilot_scenario_service import CopilotScenarioService

    scenario = await CopilotScenarioService(session).getByCode(dto.scenario_code)
    if scenario.agent_code != dto.agent_code:
        raise ValidationError(
            f"场景 {dto.scenario_code} 钉定的 Agent 是 {scenario.agent_code}，"
            f"与请求的 {dto.agent_code} 不一致"
        )
    return renderScenarioQuestion(
        template=scenario.question_template,
        slots=dto.slots,
        slotSchema=scenario.slot_schema,
        message=dto.message,
    )


async def prepareCopilotTurn(
    session: AsyncSession,
    dto: CopilotMessageRequest,
    actor: Any,
    *,
    service: AgentOrchestrationRunService | None = None,
) -> CopilotTurnResult:
    """一轮对话的完整编排：会话 → （续跑 or 带上下文新跑）→ 落轮次。"""
    # 域前缀守卫只管**自由分支**（Copilot 不是通用 Agent 的旁路）。场景分支
    # 的 Agent 由配置钉定（_renderScenarioQuestion 强制请求值 == 配置值），
    # 配置可以绑非 PROCUREMENT_ 前缀的采购 Agent（如 SUPPLIER_RISK_AGENT）。
    if not dto.scenario_code:
        assertProcurementAgent(dto.agent_code)
    runner = service or AgentOrchestrationRunService()
    conversationId = UUID(dto.conversation_id) if dto.conversation_id else None
    conversation = await conversationStore.loadOrCreateConversation(
        session,
        conversationId=conversationId,
        actor=actor,
        agentCode=dto.agent_code,
        modelConfigId=dto.model_config_id,
    )
    turns = await conversationStore.listTurns(session, conversation.conversation_id)

    # 空消息守卫：自由文本分支必须有文字；场景分支（slots 驱动，如 chips 点击）
    # 主句由模板提供，允许空 —— 但两者都空就没有可发的东西了。
    if not dto.message.strip() and not (dto.scenario_code and dto.slots):
        raise ValidationError("消息不能为空")

    resumeRunId = await _resumeTargetRunId(session, turns, actor, runner)
    if resumeRunId is not None:
        # 续跑回复 = 用户回答追问：永远走自由文本，不做场景渲染。
        await runner.resumeRun(session, resumeRunId, AgentRunResume(input=dto.message), actor)
        await conversationStore.appendTurn(session, conversation, question=dto.message, runId=resumeRunId)
        await session.commit()
        return CopilotTurnResult(
            runId=resumeRunId,
            conversationId=str(conversation.conversation_id),
            status=RunStatus.QUEUED.value,
            resumed=True,
        )

    # 场景分支（P4）：scenarioCode + slots 同时提供 ⇒ 服务端渲染最终提问
    # （冲突让位 + 模板替换在 DB 配置驱动的一处收口）；缺 slots = 自由文本。
    runQuestion = await _renderScenarioQuestion(session, dto)

    # 数据新鲜度戳（0171）：槽位实体在 feature_value 的最新 computed_at。
    # 指标重算 ⇒ 戳变化 ⇒ 相同问题文本不再命中旧 run（数据变了就重算）。
    dataStamp = await _scenarioDataStamp(session, dto)

    # 同日同问复用（resume 优先级更高，上文已处理）：命中 ⇒ turn 指向既有
    # SUCCEEDED run，SSE 立刻吐 done —— 零 LLM 成本（2026-11-24 需求：
    # 一天多次查询数据未变化 ⇒ 后台判断复用，前台正常问话即可）。
    # 复用键 = 渲染后问题 + 数据新鲜度戳（2026-11-25 需求：指标重算后同问
    # 必须重算，不能拿旧数据答新数据的问题）。
    reusedRunId = await _sameDayReusableRunId(
        session, dto=dto, actor=actor, question=runQuestion, dataStamp=dataStamp
    )
    if reusedRunId is not None:
        await conversationStore.appendTurn(
            session, conversation, question=runQuestion, runId=reusedRunId, dataStamp=dataStamp
        )
        await session.commit()
        reusedRun = await runner.getRun(session, reusedRunId, actor)
        return CopilotTurnResult(
            runId=reusedRunId,
            conversationId=str(conversation.conversation_id),
            status=str(getattr(reusedRun.status, "value", reusedRun.status)),
            resumed=False,
        )

    history = await _history(session, turns, actor, runner)
    created = await runner.createRun(
        session,
        toRunCreate(dto, inputText=conversationStore.buildRunInput(runQuestion, history)),
        actor,
    )
    await conversationStore.appendTurn(
        session, conversation, question=runQuestion, runId=created.run_id, dataStamp=dataStamp
    )
    await session.commit()
    return CopilotTurnResult(
        runId=created.run_id,
        conversationId=str(conversation.conversation_id),
        status=str(created.status),
        resumed=False,
    )


def _stepPayload(step: AgentRunStepRead) -> dict[str, Any]:
    """轨迹步 → SSE 载荷（字段名与 `AgentRunStepRead` 的 camelCase 一致）。"""
    return {
        "stepNo": step.step_no,
        "iterationNo": step.iteration_no,
        "kind": str(getattr(step.kind, "value", step.kind)),
        "status": str(getattr(step.status, "value", step.status)),
        "action": None if step.action is None else str(getattr(step.action, "value", step.action)),
        "planSummary": step.plan_summary,
        "toolName": step.tool_name,
        "answer": step.answer,
        "errorCode": step.error_code,
        "errorMessage": step.error_message,
        "output": step.output_json,
        "promptTokens": step.prompt_tokens,
        "completionTokens": step.completion_tokens,
        "cost": step.cost,
    }


def _donePayload(run: Any) -> dict[str, Any]:
    return {
        "runId": run.run_id,
        "status": str(getattr(run.status, "value", run.status)),
        "answer": run.final_answer,
        "pauseCode": None
        if run.pause_code is None
        else str(getattr(run.pause_code, "value", run.pause_code)),
        "pauseDetail": run.pause_detail,
        "errorCode": run.error_code,
        "errorMessage": run.error_message,
        "totalTokens": run.total_tokens,
        "cost": run.cost,
        "costComplete": run.cost_complete,
        "stepsUrl": buildStepsUrl(run.run_id),
    }


async def streamCopilotRun(
    runId: str,
    actor: Any,
    *,
    sessionFactory: async_sessionmaker[AsyncSession] | None = None,
    pollInterval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    maxWaitSeconds: float = DEFAULT_MAX_WAIT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = monotonic,
) -> AsyncIterator[StreamEvent]:
    """把一次运行的过程**转发**成 SSE 事件流。

    先发 `copilot_run`（runId + 轨迹地址），再逐个发新出现的 `copilot_step`，
    终态发 `copilot_done`；连接超时发 `copilot_error`（errorType=timeout，
    运行本身不受影响）。
    """
    factory = sessionFactory or getSessionFactory()
    service = AgentOrchestrationRunService()
    seenStepNo = 0
    deadline = now() + maxWaitSeconds

    yield StreamEvent(
        EVENT_COPILOT_RUN,
        {"runId": runId, "stepsUrl": buildStepsUrl(runId)},
    )

    while True:
        async with factory() as session:
            run = await service.getRun(session, runId, actor)
            steps = await service.listSteps(
                session,
                runId,
                actor,
                afterStepNo=seenStepNo + 1,
                limit=MAX_STEPS_PER_POLL,
            )

        for step in steps:
            seenStepNo = max(seenStepNo, step.step_no)
            yield StreamEvent(EVENT_COPILOT_STEP, _stepPayload(step))

        status = getattr(run.status, "value", run.status)
        if run.status in TERMINAL_STATUSES or status in {item.value for item in TERMINAL_STATUSES}:
            yield StreamEvent(EVENT_COPILOT_DONE, _donePayload(run))
            return

        if now() >= deadline:
            yield StreamEvent(
                EVENT_COPILOT_ERROR,
                {
                    "error": MSG_COPILOT_STREAM_TIMEOUT,
                    "errorType": "timeout",
                    "runId": runId,
                    "stepsUrl": buildStepsUrl(runId),
                },
            )
            return

        await sleep(pollInterval)
