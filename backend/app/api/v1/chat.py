"""对话接口路由。

POST /api/v1/chat：主对话接口（意图识别 → NL2SQL → 图表 → 回答）。
router 自身 prefix=""，由 main.py 挂载到 /api/v1 下得到 /api/v1/chat。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.exceptions import ConflictError, DomainError, NotFoundError
from app.domain.multi_step_models import RUN_STATUS_FAILED, RUN_STATUS_RUNNING
from app.domain.schemas import (
    CamelModel,
    ChatRequest,
    ChatResponse,
    HypothesisRead,
    QuerySuggestRequest,
    QuerySuggestResponse,
)
from app.api.v1.session_guard import assertSessionOwnership
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services import multi_step_persistence, multi_step_resume
from app.services.chat_service import ChatService
from app.services.embedding_service import EmbeddingService
from app.services.hypothesis_service import listSessionHypotheses

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(getCurrentUser)])
_service = ChatService()
_embeddingService = EmbeddingService()


@router.get("/sessions/{sessionId}/hypotheses", response_model=list[HypothesisRead])
async def listHypotheses(
    sessionId: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(10, ge=1, le=50),
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[HypothesisRead]:
    """某会话最新分析假设（M7 Hypothesis Hook；created_time 倒序 limit N）。

    流式路径假设不进 SSE 帧，前端在答案流结束后调本端点取「可能原因」。
    """
    await assertSessionOwnership(session, sessionId, _user)
    rows = await listSessionHypotheses(session, sessionId, limit)
    return [HypothesisRead.model_validate(r) for r in rows]


@router.post("", response_model=ChatResponse)
@limiter.limit(rateLimitValue)
async def chat(
    request: Request,
    dto: ChatRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> ChatResponse:
    """处理一条自然语言问题，返回回答 + SQL + 图表 option + 数据。

    #207 安全修复：真实调用方（_user）透传为 Agent 运行 actor（归属审计）。
    归属守卫：追问锚点（last_plan/last_sql/last_data）按 session_id 存在
    ``session_query_state`` 里，**不校验归属就会被继承** —— 于是「拿到一个别人的
    sessionId」等于「用别人的上下文提问」。全新会话没有消息行，守卫 fail-open 放行。
    """
    await assertSessionOwnership(session, dto.sessionId, _user)
    return await _service.processMessage(dto, session, user=_user)


@router.post("/stream")
@limiter.limit(rateLimitValue)
async def chatStream(
    request: Request,
    dto: ChatRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    """SSE 流式对话：meta → sql → chart → token×N → done（失败发 error 事件）。

    DB 会话依赖在响应体完整发送后才会释放（依赖项 teardown），
    因此整个生成器可安全使用 session 提交 Token 审计与对话消息。
    #207 安全修复：真实调用方（_user）透传为 Agent 运行 actor（归属审计）。

    H4 断连兜底：`background=` 是唯一能可靠捕获「客户端中途断开」的钩子 —— 它在
    Starlette 的收敛任务组之外 await，断连时确定会跑到；而断连时生成器多半停在
    `yield` 上（不在任务栈上，`except CancelledError`/`finally` 都不触发）。钩子只
    负责调 service，事务与语义都在 service 层。
    """
    # 归属守卫必须在这里（流开始之前）：进了 eventSource 就没有 HTTP 状态码可回了。
    # 与 POST /chat 同一条理由：不加守卫就能继承别人会话的追问锚点。
    await assertSessionOwnership(session, dto.sessionId, _user)

    async def eventSource() -> AsyncIterator[str]:
        async for event in _service.processMessageStream(dto, session, user=_user):
            yield event.toSse()

    async def persistIfInterrupted() -> None:
        """响应收尾钩子：本轮已产出的部分答案若尚未落库，补写并标记 interrupted。

        正常跑完的请求在这里是空操作（落库时已解除标记）。钩子在响应完成后运行，
        吞掉异常只记日志：此时已无法改变给客户端的响应，抛出去只会污染日志。
        """
        try:
            await _service.persistInterruptedStream(dto, session)
        except Exception as exc:
            logger.exception("流式断连兜底落库失败: %s", exc)

    return StreamingResponse(
        eventSource(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(persistIfInterrupted),
    )


@router.post("/suggest", response_model=QuerySuggestResponse)
async def suggestQueries(dto: QuerySuggestRequest) -> QuerySuggestResponse:
    """基于历史查询向量检索相似问法（供前端输入联想/推荐）。

    推荐功能属锦上添花：检索失败时记录日志并返回空建议，避免前端报错。
    """
    try:
        suggestions = await _embeddingService.searchSimilarQueries(
            dto.question, topK=5, datasourceId=dto.datasourceId
        )
    except DomainError as exc:
        logger.warning("相似问法检索失败，返回空建议: %s", exc.message)
        suggestions = []
    return QuerySuggestResponse(suggestions=suggestions)


class ResumeRequest(CamelModel):
    from_step_index: int | None = None
    model_override: int | None = None
    compress_again: bool = False


async def _sealAbandonedResume(session: AsyncSession, runId: uuid.UUID) -> None:
    """续跑兜底封口：流跑完后 run 仍是 running ⇒ 没人关它，显式标失败。

    为什么需要：路由只给出 `run.question`，**重新路由的结果不一定还是多步**
    （首步这次成功了 ⇒ 单步优先策略不拆步），多步链路根本没进入，`adoptRunForResume`
    也就没被执行；也可能流中途断掉。两种情况下这条 run 都会永远停在 `running`。
    放在路由层是因为它是唯一能覆盖「一切提前退出形态」的位置。
    """
    refreshed = await multi_step_persistence.loadRun(session, runId)
    if refreshed is None or refreshed.status != RUN_STATUS_RUNNING:
        return
    await multi_step_persistence.updateRun(
        session, refreshed, status=RUN_STATUS_FAILED, finished=True,
        errorSummary="续跑未走多步链路（被重新路由为单步或流中断），run 已显式封口",
    )
    await session.commit()


@router.post("/multi-step/{runId}/resume")
async def resumeMultiStep(
    request: Request,
    runId: uuid.UUID,
    dto: ResumeRequest,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    run = await multi_step_persistence.loadRun(session, runId)
    if run is None:
        raise NotFoundError(f"multi-step run {runId} 不存在")
    await assertSessionOwnership(session, str(run.session_id), _user)

    if run.datasource_id is None:
        # ChatRequest.datasourceId 是必填 int；缺了它只能 500，不如显式 409。
        raise ConflictError("该 multi-step run 没有数据源快照，无法续跑")

    idempotencyKey = request.headers.get("Idempotency-Key")
    try:
        await multi_step_resume.prepareResume(
            session, runId=runId, fromStepIndex=dto.from_step_index,
            idempotencyKey=idempotencyKey,
        )
    except multi_step_resume.ResumeNotAllowed as exc:
        # 上面已查过一次 run；这里兜的是查完与被删之间的竞态。不兜就是一个
        # 未捕获的领域异常 ⇒ 500，而正确答案是 404。
        raise NotFoundError(str(exc)) from exc
    except multi_step_resume.ResumeConflict as exc:
        raise ConflictError(str(exc)) from exc

    # 起始步**不**通过 DTO 传递：prepareResume 已把它写进 run.current_step_idx，
    # Task 6 的 adoptRunForResume 从那里读。唯一事实来源 = DB。
    chatDto = ChatRequest(
        question=run.question,
        sessionId=str(run.session_id),
        datasourceId=run.datasource_id,
        modelId=dto.model_override or run.model_id,
        resumeRunId=run.id,   # 字段类型是 uuid.UUID | None，直接给 UUID（Task 6 判决后）
    )

    async def eventSource() -> AsyncIterator[str]:
        try:
            async for event in _service.processMessageStream(chatDto, session, user=_user):
                yield event.toSse()
        finally:
            # finally 而非「循环后」：客户端断连时生成器被取消，CancelledError 也会
            # 走到这里，run 照样被封口（H4 断连落库那一课）。
            await _sealAbandonedResume(session, runId)

    return StreamingResponse(
        eventSource(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
