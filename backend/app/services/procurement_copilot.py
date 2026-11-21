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
from time import monotonic
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_schemas import (
    AgentRunCreate,
    AgentRunCreated,
    AgentRunStepRead,
)
from app.domain.exceptions import ValidationError
from app.domain.procurement_copilot_schemas import (
    PROCUREMENT_AGENT_PREFIX,
    CopilotMessageRequest,
)
from app.infrastructure.database import getSessionFactory
from app.services.agent_orchestration_run_service import (
    AgentOrchestrationRunService,
)
from app.services.agent_orchestration_types import RunStatus
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
        raise ValidationError(
            MSG_COPILOT_NOT_PROCUREMENT_AGENT.format(code=agentCode)
        )


def toRunCreate(dto: CopilotMessageRequest) -> AgentRunCreate:
    """Copilot 请求 → 编排运行请求（**一对一**，不加语义）。"""
    return AgentRunCreate(
        agent_code=dto.agent_code,
        input=dto.message,
        model_config_id=dto.model_config_id,
        limits=dto.limits,
    )


async def createCopilotRun(
    session: AsyncSession,
    dto: CopilotMessageRequest,
    actor: Any,
    *,
    service: AgentOrchestrationRunService | None = None,
) -> AgentRunCreated:
    """校验域前缀 → 复用 `createRun` 落一个 QUEUED 运行。"""
    assertProcurementAgent(dto.agent_code)
    runner = service or AgentOrchestrationRunService()
    return await runner.createRun(session, toRunCreate(dto), actor)


def _stepPayload(step: AgentRunStepRead) -> dict[str, Any]:
    """轨迹步 → SSE 载荷（字段名与 `AgentRunStepRead` 的 camelCase 一致）。"""
    return {
        "stepNo": step.step_no,
        "iterationNo": step.iteration_no,
        "kind": str(getattr(step.kind, "value", step.kind)),
        "status": str(getattr(step.status, "value", step.status)),
        "action": None
        if step.action is None
        else str(getattr(step.action, "value", step.action)),
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
        if run.status in TERMINAL_STATUSES or status in {
            item.value for item in TERMINAL_STATUSES
        }:
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
