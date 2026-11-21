"""I3-a 采购 Copilot 统一对话入口：`POST /api/v1/procurement/copilot/messages`。

设计稿 §8.4：「采购 Copilot 统一对话入口；返回 SSE 或任务引用」。

两种返回形态由请求体 `mode` 显式选择：

- `mode=task`   → **202** `{runId, status, stepsUrl}`（任务引用，调用方自己轮询轨迹）
- `mode=stream` → **SSE**：`copilot_run` → `copilot_step`* → `copilot_done`
  （超时 `copilot_error`，errorType=timeout）

前置校验（域前缀、Agent ACTIVE、profile enabled、model_config、预算上限、
语义钉住）**全部复用** `AgentOrchestrationRunService.createRun`——Copilot 是
入口，不是第二条执行链。因此创建失败时返回的是编排服务本来的 404/409/422，
而不是 Copilot 自己编一套。
"""

# ruff: noqa: B008 -- 全仓 API 层统一使用 FastAPI 的 Depends 默认参数写法。

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import DomainError
from app.domain.procurement_copilot_schemas import (
    CopilotMessageRequest,
    CopilotTaskRead,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.procurement_copilot import (
    EVENT_COPILOT_ERROR,
    CopilotStreamOptions,
    buildStepsUrl,
    copilotStreamOptions,
    createCopilotRun,
    streamCopilotRun,
)
from app.services.stream_events import StreamEvent

router = APIRouter(tags=["procurement-copilot"])

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _taskResponse(runId: str, runStatus: str) -> CopilotTaskRead:
    return CopilotTaskRead(
        run_id=runId,
        status=runStatus,
        steps_url=buildStepsUrl(runId),
    )


@router.post(
    "/copilot/messages",
    response_model=CopilotTaskRead | None,
    status_code=status.HTTP_202_ACCEPTED,
    summary="采购 Copilot 对话入口（SSE 过程流 / 任务引用）",
)
@limiter.limit(rateLimitValue)
async def postCopilotMessage(
    request: Request,
    response: Response,
    dto: CopilotMessageRequest,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    streamOptions: CopilotStreamOptions = Depends(copilotStreamOptions),
) -> CopilotTaskRead | StreamingResponse:
    """一句采购问题 → 一次采购域只读 Agent 运行（过程流 / 任务引用）。"""
    # 先创建：创建期的领域错误（404/409/422）必须以**HTTP 状态码**返回，
    # 不能在 SSE 已经开始吐字节之后才变成一条 error 事件。
    created = await createCopilotRun(session, dto, currentUser)

    if dto.mode == "task":
        return _taskResponse(created.run_id, str(created.status))

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in streamCopilotRun(
                created.run_id,
                currentUser,
                pollInterval=streamOptions.pollInterval,
                maxWaitSeconds=streamOptions.maxWaitSeconds,
            ):
                yield event.toSse()
        except DomainError as exc:
            yield StreamEvent(
                EVENT_COPILOT_ERROR,
                {
                    "error": exc.message,
                    "errorType": "domain",
                    "runId": created.run_id,
                    "stepsUrl": buildStepsUrl(created.run_id),
                },
            ).toSse()

    # SSE 走 StreamingResponse；202 只适用于 task 模式（响应模型因此可选）。
    response.status_code = status.HTTP_200_OK
    return StreamingResponse(
        eventSource(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )
