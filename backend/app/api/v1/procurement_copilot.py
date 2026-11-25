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

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import DomainError, NotFoundError
from app.domain.procurement_copilot_schemas import (
    CopilotConversationRead,
    CopilotConversationSummary,
    CopilotMessageRequest,
    CopilotPinRequest,
    CopilotScenarioRead,
    CopilotTaskRead,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.procurement_copilot import (
    EVENT_COPILOT_ERROR,
    CopilotStreamOptions,
    buildStepsUrl,
    copilotStreamOptions,
    prepareCopilotTurn,
    streamCopilotRun,
)
from app.services.procurement_copilot_conversation import (
    deleteOwnedConversation,
    getOwnedConversation,
    listOwnedConversations,
    listTurns,
)
from app.services.stream_events import StreamEvent

router = APIRouter(tags=["procurement-copilot"])

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _taskResponse(runId: str, runStatus: str, conversationId: str, resumed: bool) -> CopilotTaskRead:
    return CopilotTaskRead(
        run_id=runId,
        status=runStatus,
        steps_url=buildStepsUrl(runId),
        conversation_id=conversationId,
        resumed=resumed,
    )


@router.get(
    "/copilot/scenarios",
    response_model=list[CopilotScenarioRead],
    summary="Copilot 场景列表（启用项按 sort_order；required_role 门控可见性）",
)
async def listCopilotScenarios(
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[CopilotScenarioRead]:
    """卡片墙数据源（P4）：场景 = DB 配置行，非 admin 隐藏 admin 专属场景。"""
    from sqlalchemy import select

    from app.domain.copilot_scenario_models import CopilotScenario as ScenarioModel
    from app.domain.procurement_copilot_schemas import CopilotScenarioSlotRead

    userRoles = tuple(getattr(currentUser, "roles", None) or ())
    rows = (
        (
            await session.execute(
                select(ScenarioModel)
                .where(ScenarioModel.enabled.is_(True))
                .order_by(ScenarioModel.sort_order, ScenarioModel.id)
            )
        )
        .scalars()
        .all()
    )
    views: list[CopilotScenarioRead] = []
    for row in rows:
        # required_role = 可见角色（NULL=全员）；用户不具备 ⇒ 直接跳过
        # （不回 403 —— 卡片墙里「看不见」比报错更符合场景目录语义）
        if row.required_role and row.required_role not in userRoles:
            continue
        views.append(
            CopilotScenarioRead(
                scenario_code=row.scenario_code,
                name=row.name,
                description=row.description,
                icon_code=row.icon_code,
                agent_code=row.agent_code,
                slot_schema=[
                    CopilotScenarioSlotRead(
                        key=str(slot.get("key", "")),
                        type=str(slot.get("type", "")),
                        label=str(slot.get("label", "")),
                        required=bool(slot.get("required", False)),
                        conflict_keywords=[str(kw) for kw in (slot.get("conflictKeywords") or [])],
                        code_pattern=slot.get("codePattern"),
                        display_template=slot.get("displayTemplate"),
                    )
                    for slot in (row.slot_schema or [])
                ],
                question_template=row.question_template,
                chips=list(row.chips or []),
                result_renderer=row.result_renderer,
                required_role=row.required_role,
            )
        )
    return views


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
    # 先落轮次：创建期的领域错误（404/409/422）必须以**HTTP 状态码**返回，
    # 不能在 SSE 已经开始吐字节之后才变成一条 error 事件。
    turn = await prepareCopilotTurn(session, dto, currentUser)

    if dto.mode == "task":
        return _taskResponse(turn.runId, turn.status, turn.conversationId, turn.resumed)

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in streamCopilotRun(
                turn.runId,
                currentUser,
                pollInterval=streamOptions.pollInterval,
                maxWaitSeconds=streamOptions.maxWaitSeconds,
            ):
                if event.event == "copilot_run":
                    # 会话 id 只在这里出现一次（后续步骤/终态不必重复）
                    event = StreamEvent(
                        event.event,
                        {
                            **event.data,
                            "conversationId": turn.conversationId,
                            "resumed": turn.resumed,
                        },
                    )
                yield event.toSse()
        except DomainError as exc:
            yield StreamEvent(
                EVENT_COPILOT_ERROR,
                {
                    "error": exc.message,
                    "errorType": "domain",
                    "runId": turn.runId,
                    "stepsUrl": buildStepsUrl(turn.runId),
                },
            ).toSse()

    # SSE 走 StreamingResponse；202 只适用于 task 模式（响应模型因此可选）。
    response.status_code = status.HTTP_200_OK
    return StreamingResponse(
        eventSource(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.get(
    "/copilot/conversations",
    response_model=list[CopilotConversationSummary],
    summary="多轮 Copilot 会话列表（当前用户，最近更新在前）",
)
async def listCopilotConversations(
    limit: int = Query(default=20, ge=1, le=50),
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[CopilotConversationSummary]:
    """侧栏「历史会话」的数据源：只列**自己的**会话 + 轮数 + 最后一问。"""
    rows = await listOwnedConversations(session, currentUser, limit=limit)
    return [
        CopilotConversationSummary(
            conversation_id=str(row.conversation.conversation_id),
            agent_code=row.conversation.agent_code,
            model_config_id=row.conversation.model_config_id,
            turn_count=row.turnCount,
            last_question=row.lastQuestion,
            updated_at=row.conversation.updated_time.isoformat(),
            pinned_at=None if row.conversation.pinned_at is None else row.conversation.pinned_at.isoformat(),
        )
        for row in rows
    ]


@router.post(
    "/copilot/conversations/{conversationId}/pin",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="置顶/取消置顶 Copilot 会话（创建者；pinned_at 跨设备同步）",
)
async def pinCopilotConversation(
    conversationId: str,
    dto: CopilotPinRequest,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> Response:
    """置顶状态落库（pinned_at 列）；别人的会话 404。"""
    from uuid import UUID

    from app.services.procurement_copilot_conversation import setPinnedConversation

    try:
        parsedId = UUID(conversationId)
    except ValueError as exc:
        raise NotFoundError("会话不存在或无权访问") from exc
    await setPinnedConversation(session, parsedId, currentUser, pinned=dto.pinned)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/copilot/conversations/{conversationId}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除多轮 Copilot 会话（创建者）",
)
async def deleteCopilotConversation(
    conversationId: str,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> Response:
    """删除会话及其轮次；别人的会话 404（不泄露是否存在）。"""
    from uuid import UUID

    try:
        parsedId = UUID(conversationId)
    except ValueError as exc:
        raise NotFoundError("会话不存在或无权访问") from exc
    await deleteOwnedConversation(session, parsedId, currentUser)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/copilot/conversations/{conversationId}",
    response_model=CopilotConversationRead,
    summary="多轮 Copilot 会话详情（创建者或 admin）",
)
async def getCopilotConversation(
    conversationId: str,
    currentUser: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> CopilotConversationRead:
    """会话 + 每轮的运行状态/答案（刷新页面后恢复线程用）。"""
    from uuid import UUID

    from app.domain.procurement_copilot_schemas import (
        CopilotConversationRead,
        CopilotTurnRead,
    )
    from app.services.agent_orchestration_run_service import (
        AgentOrchestrationRunService,
    )

    try:
        parsedId = UUID(conversationId)
    except ValueError as exc:
        raise NotFoundError("会话不存在或无权访问") from exc
    conversation = await getOwnedConversation(session, parsedId, currentUser)
    turns = await listTurns(session, parsedId)
    runService = AgentOrchestrationRunService()
    views: list[CopilotTurnRead] = []
    for turn in turns:
        try:
            run = await runService.getRun(session, turn.run_id, currentUser)
        except DomainError:
            continue
        views.append(
            CopilotTurnRead(
                seq=turn.seq,
                question=turn.question,
                run_id=turn.run_id,
                status=str(getattr(run.status, "value", run.status)),
                answer=run.final_answer,
                pause_code=None
                if run.pause_code is None
                else str(getattr(run.pause_code, "value", run.pause_code)),
                created_at=turn.created_time.isoformat(),
            )
        )
    return CopilotConversationRead(
        conversation_id=str(conversation.conversation_id),
        agent_code=conversation.agent_code,
        model_config_id=conversation.model_config_id,
        turns=tuple(views),
    )
