"""会话历史接口路由。

包含两类：
- Token 用量看板（既有）：
    GET /api/v1/sessions                          会话列表（聚合统计）
    GET /api/v1/sessions/usage/global             全局用量摘要
    GET /api/v1/sessions/usage/daily              按天趋势
    GET /api/v1/sessions/usage/by-model           按模型汇总
    GET /api/v1/sessions/{sessionId}/usage        单会话用量摘要
    GET /api/v1/sessions/{sessionId}/usage/list   单会话用量流水
- 聊天语义历史（新增）：
    GET    /api/v1/sessions/chat-history          会话列表（按消息聚合，UI 历史面板用）
    GET    /api/v1/sessions/{sessionId}/messages  单会话消息流加载
    DELETE /api/v1/sessions/{sessionId}           硬删除（message + token_usage + query_state）

路由顺序约束：
- 字面量段（/chat-history, /usage/global 等）必须在 /{sessionId} 之前，避免被路径参数吞掉。
- /{sessionId}/messages 与 /{sessionId}/usage 同属「单会话读」类，并列在末尾即可。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getCurrentUser
from app.domain.error_messages import (
    MSG_EXPORT_MESSAGE_NOT_FOUND,
    MSG_EXPORT_SESSION_EMPTY,
    MSG_HISTORY_DELETE_NOT_FOUND,
    MSG_HISTORY_EXPORT_CONTENT_DISPOSITION,
    MSG_HISTORY_EXPORT_FILENAME,
    MSG_HISTORY_EXPORT_MESSAGE_FILENAME,
    MSG_HISTORY_LISTING_LIMIT,
    MSG_HISTORY_LISTING_OFFSET,
    MSG_HISTORY_MESSAGES_BEFORE_ID,
    MSG_HISTORY_MESSAGES_LIMIT,
)
from app.domain.exceptions import NotFoundError
from app.domain.schemas import (
    ChatSessionListItem,
    DailyUsageTrend,
    GlobalUsageSummary,
    ModelUsageAggregate,
    SessionListItem,
    SessionMessagesResponse,
    SessionTokenUsageRead,
    TokenUsageSummary,
)
from app.infrastructure.database import getDb
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.pdf_export_service import ChatExportPdfBuilder
from app.services.session_history_service import SessionHistoryService
from app.services.token_usage_service import TokenUsageService

router = APIRouter(dependencies=[Depends(getCurrentUser)])


@router.get("", response_model=list[SessionListItem])
async def listSessions(session: AsyncSession = Depends(getDb)) -> list[SessionListItem]:
    """列出所有有 Token 流水的会话（聚合统计 + 最近活动），用于用量看板。"""
    svc = TokenUsageService()
    return await svc.listSessions(session)


# 全局用量聚合路由：声明在 /{sessionId}/... 之前，避免被路径参数捕获
@router.get("/usage/global", response_model=GlobalUsageSummary)
async def getGlobalUsageSummary(
    session: AsyncSession = Depends(getDb),
) -> GlobalUsageSummary:
    """跨全部会话的全局用量摘要（用量看板顶部卡片）。"""
    svc = TokenUsageService()
    return await svc.getGlobalSummary(session)


@router.get("/usage/daily", response_model=list[DailyUsageTrend])
async def getDailyUsageTrends(
    session: AsyncSession = Depends(getDb),
) -> list[DailyUsageTrend]:
    """近 30 天按天聚合的全局用量趋势（升序）。"""
    svc = TokenUsageService()
    return await svc.getDailyTrends(session)


@router.get("/usage/by-model", response_model=list[ModelUsageAggregate])
async def getModelUsage(
    session: AsyncSession = Depends(getDb),
) -> list[ModelUsageAggregate]:
    """按模型汇总的全局用量。"""
    svc = TokenUsageService()
    return await svc.getModelUsage(session)


# 聊天语义会话列表（字面量段，必须在 /{sessionId}/* 之前）
@router.get(
    "/chat-history",
    response_model=list[ChatSessionListItem],
)
async def listChatHistory(
    session: AsyncSession = Depends(getDb),
    limit: int = Query(default=50, ge=1, le=200, description=MSG_HISTORY_LISTING_LIMIT),
    offset: int = Query(default=0, ge=0, description=MSG_HISTORY_LISTING_OFFSET),
) -> list[ChatSessionListItem]:
    """列出有消息的聊天会话（按最后活跃时间倒序），用于智能问答界面右侧历史面板。

    与 GET /api/v1/sessions（用量视角）解耦：前者按 session_message 聚合，
    后者按 session_token_usage 聚合。

    本期未强制 tenant 过滤（与 /api/v1/chat 行为一致）；多租户部署需在
    service 层按 tenant_id 过滤，参数预留可在 SessionHistoryService 扩展。
    """
    svc = SessionHistoryService()
    return await svc.listChatSessions(session, limit=limit, offset=offset)


@router.get("/{sessionId}/usage", response_model=TokenUsageSummary)
async def getSessionUsage(sessionId: str, session: AsyncSession = Depends(getDb)) -> TokenUsageSummary:
    svc = TokenUsageService()
    return await svc.summarize(session, sessionId)


@router.get("/{sessionId}/usage/list", response_model=list[SessionTokenUsageRead])
async def getSessionUsageList(
    sessionId: str, session: AsyncSession = Depends(getDb)
) -> list[SessionTokenUsageRead]:
    svc = TokenUsageService()
    usages = await svc.getUsageBySession(session, sessionId)
    return [SessionTokenUsageRead.model_validate(u) for u in usages]


@router.get(
    "/{sessionId}/messages",
    response_model=SessionMessagesResponse,
)
async def getSessionMessages(
    sessionId: str,
    session: AsyncSession = Depends(getDb),
    limit: int = Query(default=200, ge=1, le=1000, description=MSG_HISTORY_MESSAGES_LIMIT),
    before_id: int | None = Query(default=None, alias="before_id", description=MSG_HISTORY_MESSAGES_BEFORE_ID),
) -> SessionMessagesResponse:
    """加载某 session 的完整消息流（按时间正序，user → assistant 交错）。

    不存在的 sessionId 返 200 + 空 messages（前端便于无副作用切换）。
    chartOption/data 未持久化，历史回放仅展示 content + sql + 时间戳。
    """
    svc = SessionHistoryService()
    return await svc.loadFullMessages(
        session, sessionId, limit=limit, beforeId=before_id
    )


@router.delete("/{sessionId}", status_code=204)
async def deleteSessionHistory(
    sessionId: str,
    session: AsyncSession = Depends(getDb),
) -> Response:
    """硬删除某 session 的所有数据（message + token_usage + query_state 三表）。

    sessionId 不存在或三表均无数据 → 404。删除为单事务，任一失败回滚。
    """
    svc = SessionHistoryService()
    total = await svc.deleteSessionHistory(session, sessionId)
    if total == 0:
        raise NotFoundError(
            message=MSG_HISTORY_DELETE_NOT_FOUND,
            detail=f"sessionId={sessionId}",
        )
    return Response(status_code=204)


@router.get(
    "/{sessionId}/export.pdf",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}},
        404: {"description": MSG_EXPORT_SESSION_EMPTY + " / " + MSG_EXPORT_MESSAGE_NOT_FOUND},
    },
)
@limiter.limit(rateLimitValue)
async def exportSessionPdf(
    request: Request,
    sessionId: str,
    session: AsyncSession = Depends(getDb),
    message_id: int | None = Query(default=None, alias="message_id", ge=1),
) -> Response:
    """导出某 session 的问答记录为 PDF。

    Query:
        message_id: 非空时只导出该 assistant 消息 + 上一条 user 消息（按 id 升序）。
                    必须属于该 session；否则 404。

    响应：application/pdf（attachment 触发下载）；文件名包含 sessionId 或 messageId。
    会话无任何消息 → 404（与 DELETE 行为对齐）。
    """
    svc = SessionHistoryService()
    try:
        payload = await svc.buildExportPayload(session, sessionId, messageId=message_id)
    except ValueError:
        # 安全审查 HIGH-3：404 detail 不回显 sessionId/messageId，避免枚举攻击
        raise NotFoundError(message=MSG_EXPORT_MESSAGE_NOT_FOUND) from None
    if payload is None:
        # 安全审查 HIGH-3：404 detail 不回显 sessionId，避免枚举攻击
        raise NotFoundError(message=MSG_EXPORT_SESSION_EMPTY)

    builder = ChatExportPdfBuilder()
    pdf_bytes = builder.build(payload)

    filename_template = (
        MSG_HISTORY_EXPORT_MESSAGE_FILENAME if message_id is not None else MSG_HISTORY_EXPORT_FILENAME
    )
    filename = filename_template.format(
        sessionId=sessionId if message_id is None else f"{sessionId}",
        messageId=message_id,
    )
    disposition = MSG_HISTORY_EXPORT_CONTENT_DISPOSITION.format(filename=filename)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )