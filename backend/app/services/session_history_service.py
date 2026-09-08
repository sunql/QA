"""会话历史服务（聊天语义维度）。

与 chat_service 解耦，专责 session_message 聚合、加载、级联删除。
本服务暴露的方法供 api/v1/session.py 中的 3 个新端点调用：
- listChatSessions    → GET /api/v1/sessions/chat-history
- loadFullMessages    → GET /api/v1/sessions/{sessionId}/messages
- deleteSessionHistory → DELETE /api/v1/sessions/{sessionId}

SQL 聚合策略：
- 列表：一次聚合 + 一次批量取「最后 user/assistant」内容，避免 N+1。
- 消息流：单 SELECT id ASC + LIMIT，与现有 _loadRecentRounds（id DESC）方向相反。
- 删除：三表独立 DELETE，单事务；返回删除总行数供 controller 判 404。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SessionMessage, SessionQueryState, SessionTokenUsage
from app.domain.schemas import (
    ChatMessageRead,
    ChatSessionListItem,
    SessionMessagesResponse,
)
from app.services.pdf_export_service import ChatExportPayload, ChatExportTurn

logger = logging.getLogger(__name__)


# Last question/answer preview 截断长度（前端列表展示用，避免首屏 DOM 溢出）
_QUESTION_PREVIEW_LIMIT = 30
_ANSWER_PREVIEW_LIMIT = 100

# PDF 导出安全上限：单 session turns 与单条 content 字节数封顶，防止恶意构造
# 大 markdown / 长 session 导致 PDF 渲染 OOM 或 CPU 燃尽。limit 由 controller 透传。
_MAX_TURNS_PER_EXPORT = 500
_MAX_CONTENT_BYTES = 50_000


class SessionHistoryService:
    """聊天会话历史读 + 写（删除）服务。"""

    async def listChatSessions(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        channel: str = "chat",
    ) -> list[ChatSessionListItem]:
        """列出有消息的会话，按最后活跃时间倒序。

        只包含 session_message 表中至少一行的 sessionId（无消息的纯 Token 用量
        会话不展示 —— 用量看板已覆盖，见 SessionListItem）。

        channel 取值 ``chat`` / ``doc_qa``，用于按渠道隔离历史面板；默认 ``chat``
        保持既有聊天行为不变。controller 边界用 ``Query(pattern=...)`` 拦截
        非法值。

        limit/offset 在 controller 边界做 1-200/0+ clamp，service 层信任入参。
        """
        # 主聚合：每 session 一行（首/末时间、消息数）
        agg = (
            select(
                SessionMessage.session_id.label("session_id"),
                func.min(SessionMessage.created_time).label("first_time"),
                func.max(SessionMessage.created_time).label("last_time"),
                func.count().label("message_count"),
            )
            .where(SessionMessage.channel == channel)
            .group_by(SessionMessage.session_id)
            .order_by(func.max(SessionMessage.created_time).desc())
            .limit(limit)
            .offset(offset)
        )
        agg_rows = (await session.execute(agg)).all()
        if not agg_rows:
            return []

        session_ids = [r.session_id for r in agg_rows]
        last_user_map = await self._buildLastMessageContentMap(
            session, session_ids, role="user"
        )
        last_asst_map = await self._buildLastMessageContentMap(
            session, session_ids, role="assistant"
        )

        out: list[ChatSessionListItem] = []
        for r in agg_rows:
            last_q = last_user_map.get(r.session_id)
            last_a = last_asst_map.get(r.session_id)
            # SessionMessage.content 在模型上为非空，但保留 None 防御（迁移历史脏数据）
            out.append(
                ChatSessionListItem(
                    session_id=r.session_id,
                    first_time=r.first_time,
                    last_time=r.last_time,
                    message_count=int(r.message_count or 0),
                    last_question=(
                        last_q[:_QUESTION_PREVIEW_LIMIT] if last_q else None
                    ),
                    last_answer_preview=(
                        last_a[:_ANSWER_PREVIEW_LIMIT] if last_a else None
                    ),
                )
            )
        return out

    async def _buildLastMessageContentMap(
        self,
        session: AsyncSession,
        sessionIds: list[str],
        *,
        role: str,
    ) -> dict[str, str]:
        """批量取每个 session 最后一条指定 role 消息的 content。

        用 MAX(id) 决胜（id 主键唯一单调，比 MAX(created_time) 更稳——
        后者同时间戳并列会返回多行或丢行）。未命中 role 的 session 不在返回字典里。
        """
        max_id_sub = (
            select(
                SessionMessage.session_id.label("sid"),
                func.max(SessionMessage.id).label("max_id"),
            )
            .where(
                and_(
                    SessionMessage.role == role,
                    SessionMessage.session_id.in_(sessionIds),
                )
            )
            .group_by(SessionMessage.session_id)
            .subquery()
        )
        content_q = select(
            SessionMessage.session_id,
            SessionMessage.content,
        ).join(
            max_id_sub,
            and_(
                SessionMessage.session_id == max_id_sub.c.sid,
                SessionMessage.id == max_id_sub.c.max_id,
            ),
        )
        return {
            r.session_id: r.content
            for r in (await session.execute(content_q)).all()
            if r.content is not None
        }

    async def loadFullMessages(
        self,
        session: AsyncSession,
        sessionId: str,
        *,
        limit: int = 200,
        beforeId: int | None = None,
    ) -> SessionMessagesResponse:
        """加载某 session 的完整消息流（按时间正序，user → assistant 交错）。

        与现有 _loadRecentRounds（chat_service.py:2024）方向相反：历史面板需要
        从最早开始展示，LIMIT N + before_id cursor 即可追加语义稳定的分页。

        limit 边界由 controller clamp（1-1000），service 层信任入参。
        """
        stmt = (
            select(
                SessionMessage.id,
                SessionMessage.role,
                SessionMessage.content,
                SessionMessage.question,
                SessionMessage.sql_generated,
                SessionMessage.created_time,
            )
            .where(SessionMessage.session_id == sessionId)
            .order_by(SessionMessage.id.asc())
            .limit(limit)
        )
        if beforeId is not None:
            stmt = stmt.where(SessionMessage.id < beforeId)
        rows = (await session.execute(stmt)).all()

        msgs = [
            ChatMessageRead(
                id=r.id,
                role=r.role,
                content=r.content,
                question=r.question if r.role == "user" else None,
                sql=r.sql_generated if r.role == "assistant" else None,
                created_time=r.created_time,
            )
            for r in rows
        ]
        return SessionMessagesResponse(session_id=sessionId, messages=msgs)

    async def deleteSessionHistory(
        self,
        session: AsyncSession,
        sessionId: str,
    ) -> int:
        """硬删除某 session 的所有相关数据，返回总删除行数。

        级联范围：session_message + session_token_usage + session_query_state。
        单事务三表 DELETE，任一失败抛异常回滚；正常完成返回 sum(实际删除行数)。

        controller 端按 0 判 404：意味着 session 在三表中均无数据（不存在）。

        实现细节：用 RETURNING 取实际删除的 id 数，避免依赖 cursor.rowcount
        （asyncpg 在某些版本上 DELETE rowcount 返回 -1，会让 service 误判 404）。
        """
        msg_result = await session.execute(
            delete(SessionMessage)
            .where(SessionMessage.session_id == sessionId)
            .returning(SessionMessage.id)
        )
        msg_ids = msg_result.scalars().all()

        usage_result = await session.execute(
            delete(SessionTokenUsage)
            .where(SessionTokenUsage.session_id == sessionId)
            .returning(SessionTokenUsage.id)
        )
        usage_ids = usage_result.scalars().all()

        qs_result = await session.execute(
            delete(SessionQueryState)
            .where(SessionQueryState.session_id == sessionId)
            .returning(SessionQueryState.id)
        )
        qs_ids = qs_result.scalars().all()

        await session.commit()

        msg_count = len(msg_ids)
        usage_count = len(usage_ids)
        qs_count = len(qs_ids)
        total = msg_count + usage_count + qs_count
        if total:
            logger.info(
                "删除会话历史: session=%s message=%d usage=%d query_state=%d",
                sessionId,
                msg_count,
                usage_count,
                qs_count,
            )
        return total

    async def buildExportPayload(
        self,
        session: AsyncSession,
        sessionId: str,
        *,
        messageId: int | None = None,
    ) -> ChatExportPayload | None:
        """构建 PDF 导出的不可变 payload。

        行为约定：
        - ``messageId`` 为 None：导出该 session 的全部 user/assistant 消息，按 id ASC。
        - ``messageId`` 非 None：导出该 session 中 id == messageId 的 assistant
          消息 + 其紧邻的上一条 user 消息（按 created_time 升序）；若 messageId
          不属于该 session 抛 ValueError（controller 转 404）。
        - 返回 None 表示该 session 没有任何消息（无 markdown 可渲染）。

        元信息（model_name/tokens/cost）来自 ``session_token_usage`` 表：
        按 ``session_id + request_time <= assistant_message.created_time`` 取
        最近的 1 条；usage 与 message 无 FK 时序对齐启发式，精度受历史污染影响，
        PDF 注明「best-effort」由读者自行解读（不阻塞导出）。

        图表类型：``SessionMessage`` 当前无 chart_type 列；本期先省略 chart 区域。
        """
        if messageId is not None:
            return await self._buildSingleTurnPayload(session, sessionId, messageId)
        return await self._buildFullSessionPayload(session, sessionId)

    async def _buildFullSessionPayload(
        self,
        session: AsyncSession,
        sessionId: str,
    ) -> ChatExportPayload | None:
        """加载整 session 消息并按 user/assistant 交错配对。

        安全护栏：turns 数量 + 单条 content 字节数受 ``_MAX_TURNS_PER_EXPORT`` /
        ``_MAX_CONTENT_BYTES`` 限制，避免超大 session 触发 PDF 渲染 OOM（审查 HIGH-3）。
        超限内容截断并以 … 标记，便于阅读端识别。
        """
        stmt = (
            select(
                SessionMessage.id,
                SessionMessage.role,
                SessionMessage.content,
                SessionMessage.created_time,
                SessionMessage.sql_generated,
            )
            .where(SessionMessage.session_id == sessionId)
            .order_by(SessionMessage.id.asc())
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return None

        turn_dict = self._pairMessagesToTurns(rows)
        # 仅取最近 N 轮，超出截断（按 created_time DESC 取，再 reverse 回 ASC）
        if len(turn_dict) > _MAX_TURNS_PER_EXPORT:
            turn_dict = turn_dict[-_MAX_TURNS_PER_EXPORT:]
        usage_map = await self._fetchUsageMetaByMessage(session, sessionId, [r.id for r in rows])

        turns = [
            ChatExportTurn(
                user_content=self._truncate(user.content),
                user_time=user.created_time,
                assistant_content=self._truncate(asst.content),
                assistant_time=asst.created_time,
                sql=asst.sql_generated,
                model_name=usage_map.get(asst.id, {}).get("model_name"),
                tokens_used=usage_map.get(asst.id, {}).get("tokens_used"),
                cost=usage_map.get(asst.id, {}).get("cost"),
            )
            for user, asst in turn_dict
        ]
        title = self._deriveTitle(turns)
        return ChatExportPayload(
            session_id=sessionId,
            title=title,
            generated_at=datetime.now(),
            turns=turns,
        )

    async def _buildSingleTurnPayload(
        self,
        session: AsyncSession,
        sessionId: str,
        messageId: int,
    ) -> ChatExportPayload | None:
        """单条 assistant + 紧邻上一条 user。"""
        target_stmt = select(
            SessionMessage.id,
            SessionMessage.role,
            SessionMessage.content,
            SessionMessage.created_time,
            SessionMessage.sql_generated,
        ).where(
            and_(
                SessionMessage.session_id == sessionId,
                SessionMessage.id == messageId,
            )
        )
        target = (await session.execute(target_stmt)).first()
        if target is None:
            # messageId 不属于该 session，controller 转 404
            raise ValueError(f"message_id {messageId} 不属于 session {sessionId}")
        # 单条导出按"该 assistant + 上一条 user"展示；若 messageId 是 user 行，则只取自己
        if target.role == "user":
            asst_row = None
            user_row = target
        else:
            asst_row = target
            prev_stmt = (
                select(
                    SessionMessage.id,
                    SessionMessage.role,
                    SessionMessage.content,
                    SessionMessage.created_time,
                    SessionMessage.sql_generated,
                )
                .where(
                    and_(
                        SessionMessage.session_id == sessionId,
                        SessionMessage.role == "user",
                        SessionMessage.id < messageId,
                    )
                )
                .order_by(SessionMessage.id.desc())
                .limit(1)
            )
            user_row = (await session.execute(prev_stmt)).first()

        if asst_row is None:
            # 仅有 user 行，PDF 只展示该条问答的前半部分
            turns = [
                ChatExportTurn(
                    user_content=self._truncate(user_row.content) if user_row else "",
                    user_time=user_row.created_time if user_row else None,
                    assistant_content="（该 user 消息后无 assistant 回答）",
                    assistant_time=None,
                )
            ]
        else:
            usage_map = await self._fetchUsageMetaByMessage(session, sessionId, [asst_row.id])
            meta = usage_map.get(asst_row.id, {})
            turns = [
                ChatExportTurn(
                    user_content=self._truncate(user_row.content) if user_row else "",
                    user_time=user_row.created_time if user_row else None,
                    assistant_content=self._truncate(asst_row.content),
                    assistant_time=asst_row.created_time,
                    sql=asst_row.sql_generated,
                    model_name=meta.get("model_name"),
                    tokens_used=meta.get("tokens_used"),
                    cost=meta.get("cost"),
                )
            ]
        title = self._deriveTitle(turns)
        return ChatExportPayload(
            session_id=sessionId,
            title=title,
            generated_at=datetime.now(),
            turns=turns,
        )

    @staticmethod
    def _pairMessagesToTurns(
        rows: list[Any],
    ) -> list[tuple[Any, Any]]:
        """把按时间正序的 user/assistant 消息配对成 (user, assistant) 元组。

        规则：扫描每个 assistant 消息，把它与「最近一条出现在它之前的 user 消息」配对。
        若某 assistant 之前无 user（如会话首条就是 assistant），用空 user 配对。
        末尾未配对的 user 消息丢弃（按 chat 语义仅展示完整问答轮次）。
        """
        pairs: list[tuple[Any, Any]] = []
        last_user: Any = None
        for r in rows:
            if r.role == "user":
                last_user = r
            elif r.role == "assistant":
                empty_user_sentinel = type("EmptyUser", (), {"content": "", "created_time": None, "id": None})()
                pairs.append((last_user if last_user is not None else empty_user_sentinel, r))
                last_user = None  # 配对后清空，避免下一轮复用同一 user
        return pairs

    @staticmethod
    async def _fetchUsageMetaByMessage(
        session: AsyncSession,
        sessionId: str,
        assistantMessageIds: list[int],
    ) -> dict[int, dict[str, Any]]:
        """按 assistant message id 取最近一条 session_token_usage 元信息。

        时序匹配启发式：取 ``session_id`` 一致 + ``request_time <= message.created_time``
        的最近 1 条。message.id 与 usage 无 FK，按 id 区间模糊匹配：把每个 assistant
        message 的 created_time 与 usage 行配对，取时间最近且不晚于 message 的那条。
        """
        if not assistantMessageIds:
            return {}
        # 先取目标 messages 的 created_time
        msg_time_stmt = select(SessionMessage.id, SessionMessage.created_time).where(
            SessionMessage.id.in_(assistantMessageIds)
        )
        msg_times = {row.id: row.created_time for row in (await session.execute(msg_time_stmt)).all()}

        # 拉该 session 全部 usage 行（量级小，session 内通常 < 200 行）
        usage_stmt = (
            select(
                SessionTokenUsage.request_time,
                SessionTokenUsage.model_name,
                SessionTokenUsage.total_tokens,
                SessionTokenUsage.cost,
            )
            .where(SessionTokenUsage.session_id == sessionId)
            .order_by(SessionTokenUsage.request_time.desc())
        )
        usages = (await session.execute(usage_stmt)).all()
        out: dict[int, dict[str, Any]] = {}
        for msg_id, msg_time in msg_times.items():
            best = None
            for u in usages:
                if u.request_time <= msg_time:
                    best = u
                    break  # usages 已按 DESC，取第一个即最近
            if best is not None:
                out[msg_id] = {
                    "model_name": best.model_name,
                    "tokens_used": int(best.total_tokens or 0),
                    "cost": Decimal(str(best.cost or 0)),
                }
        return out

    @staticmethod
    def _deriveTitle(turns: list[ChatExportTurn]) -> str:
        """取首个 user 问题（前 40 字）作为文档标题，回退到 session_id。"""
        for turn in turns:
            text = (turn.user_content or "").strip().splitlines()[0] if turn.user_content else ""
            if text:
                return text[:40] + ("…" if len(text) > 40 else "")
        return "智能问答会话导出"

    @staticmethod
    def _truncate(content: str | None) -> str:
        """截断单条 content 至 ``_MAX_CONTENT_BYTES`` 字节，超限附 … 标记。

        按字符数（str len）粗算 ≈ 字节数（中文 UTF-8 多字节会高估），保证最坏
        情况不超 _MAX_CONTENT_BYTES * 3 字节，对 PDF 渲染足够安全。
        """
        if not content:
            return ""
        if len(content) <= _MAX_CONTENT_BYTES:
            return content
        return content[:_MAX_CONTENT_BYTES] + "…"