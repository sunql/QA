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

import base64
import binascii
import logging
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.error_messages import (
    MSG_EXPORT_CHART_COUNT_EXCEEDED,
    MSG_EXPORT_CHART_IMAGE_INVALID,
    MSG_EXPORT_CHART_IMAGE_TOO_LARGE,
    MSG_EXPORT_CHART_IMAGE_TOO_MANY_PIXELS,
    MSG_EXPORT_CHART_MESSAGE_NOT_FOUND,
    MSG_EXPORT_CHART_TOTAL_TOO_LARGE,
    MSG_EXPORT_CHART_TOTAL_TOO_MANY_PIXELS,
)
from app.domain.exceptions import ValidationError
from app.domain.models import SessionMessage, SessionQueryState, SessionTokenUsage
from app.domain.schemas import (
    ChatExportChartImage,
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

# ---- 导出图表位图的抗滥用上界（0105）----------------------------------------
# 这些是**防御性上界**，不是可调策略（按《魔数治理》「判别不清」档留在源码里）：
# 新端点首次接收用户提交的二进制，没有上限就等于给了一个「用 base64 灌爆内存/
# 把 PDF 撑到无法渲染」的入口。
_MAX_EXPORT_IMAGE_BYTES = 2 * 1024 * 1024        # 单张 PNG 解码后上限
_MAX_EXPORT_CHART_IMAGES = 200                   # charts 条数上限
_MAX_EXPORT_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024  # 全部图片解码后总量上限
# 字节上限**只管压缩后的大小**，管不住解出来有多大：一张 12000×12000 的纯色 PNG
# 只有 580 KB，字节闸放行、魔数也对，交给 Pillow 却要按 1.44 亿像素分配内存（实测
# 单张峰值 RSS +2 GB）。而 PIL 默认的 MAX_IMAGE_PIXELS 只在这个量的 2 倍以上才
# 抛异常，中间这一大段是完全不设防的 —— 所以像素数必须自己卡。
# 上界取 8M px（≈4000×2000）：前端真实产物是 800×420 × pixelRatio 2 = 1.34M px，
# 留了近 6 倍余量；总量取 40M px，把一次请求的瞬时解码内存压在 ~160 MB。
_MAX_EXPORT_IMAGE_PIXELS = 8_000_000
_MAX_EXPORT_TOTAL_IMAGE_PIXELS = 40_000_000
# 只收 PNG（前端 getDataURL({type:"png"}) 的输出）。前缀 + 魔数双重校验：
# 只信前缀等于允许「把任意二进制贴个 PNG 标签」送进来。
_CHART_IMAGE_DATA_URL_PREFIX = "data:image/png;base64,"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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

        channel 取值 ``chat`` / ``doc_qa`` / ``wiki_qa``，用于按渠道隔离历史面板；
        默认 ``chat`` 保持既有聊天行为不变。controller 边界用 ``Query(pattern=...)``
        拦截非法值。

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
        tail: bool = False,
    ) -> SessionMessagesResponse:
        """加载某 session 的完整消息流（按时间正序，user → assistant 交错）。

        与现有 _loadRecentRounds（chat_service.py:2024）方向相反：历史面板需要
        从最早开始展示，LIMIT N + before_id cursor 即可追加语义稳定的分页。

        ``tail=True`` 取**最新**的 N 条（仍是正序返回）。存在的理由：导出 PDF 只
        保留**最后 500 轮**（见 ``_buildFullSessionPayload`` 的 ``turn_dict[-500:]``），
        所以要给导出配图的前端必须拿到同一个窗口。默认的「最早 N 条」在长会话里
        与那个窗口**完全不相交** —— 前端会一张图都配不上，导出里全是灰占位框，
        而且不报任何错。``beforeId`` 更具体，二者同时给时以 ``beforeId`` 为准。

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
                SessionMessage.interrupted,
                # 0105（图表进最终报告）：历史回放要能看到图。此前图只活在实时
                # 响应里，切走再切回整段图消失。
                SessionMessage.chart_type,
                SessionMessage.chart_option,
            )
            .where(SessionMessage.session_id == sessionId)
            .limit(limit)
        )
        if beforeId is not None:
            stmt = stmt.where(SessionMessage.id < beforeId).order_by(
                SessionMessage.id.asc()
            )
        elif tail:
            # 先倒序取最新 N 条，再翻回正序 —— 响应契约恒为「按时间正序」，
            # 调用方不需要知道这里绕了一下。
            stmt = stmt.order_by(SessionMessage.id.desc())
        else:
            stmt = stmt.order_by(SessionMessage.id.asc())
        rows = (await session.execute(stmt)).all()
        if beforeId is None and tail:
            rows = list(reversed(rows))

        msgs = [
            ChatMessageRead(
                id=r.id,
                role=r.role,
                content=r.content,
                question=r.question if r.role == "user" else None,
                sql=r.sql_generated if r.role == "assistant" else None,
                created_time=r.created_time,
                # H4：断连兜底写入的半截回答，前端据此渲染「（已中断）」
                interrupted=bool(r.interrupted),
                # 0105：无图的行（user 行 / 改动前的存量行）两列均为 NULL → None
                chart_type=r.chart_type,
                chart_option=r.chart_option,
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

        图表（0105）：``SessionMessage.chart_type`` / ``chart_option`` 随行带出，
        填充 ``ChatExportTurn`` 的同名字段。注意此处**只带结构不带图片**：真
        ECharts 类图需要前端在导出前离屏渲成 PNG 回传，由 controller 层把这批
        PNG 按 messageId 合进 payload（见 ``attachChartImages``）。
        """
        if messageId is not None:
            return await self._buildSingleTurnPayload(session, sessionId, messageId)
        return await self._buildFullSessionPayload(session, sessionId)

    async def resolveChartImages(
        self,
        session: AsyncSession,
        sessionId: str,
        charts: list[ChatExportChartImage],
    ) -> dict[int, bytes]:
        """校验并解码前端回传的图表位图 → ``{messageId: pngBytes}``。

        这是本服务第一次接收**用户提交的二进制**，故在边界处显式失败
        （任一条不合法 → ``ValidationError`` ⇒ 422）：

        - 条数 / 单张字节 / 总量字节三重上限（见 ``_MAX_EXPORT_*``）；
        - **单张与总量的像素上限** —— 字节闸管不住解压炸弹（见
          ``_MAX_EXPORT_IMAGE_PIXELS`` 的注释）；
        - ``data:image/png;base64,`` 前缀 + PNG 魔数（只信前缀等于允许「任意
          二进制贴个 PNG 标签」送进来）；
        - ``messageId`` 必须属于该 session —— **不信任前端传来的 id**，否则可以
          借导出把别人的图拼进来。报错不回显 id（沿用 HIGH-3 口径，否则 422
          就成了「这个 id 属不属于这个会话」的枚举探针）。

        重复 messageId 取后者：前端逐条生成不该重复，不值得为这点小事拒掉整份导出。
        """
        if len(charts) > _MAX_EXPORT_CHART_IMAGES:
            raise ValidationError(MSG_EXPORT_CHART_COUNT_EXCEEDED)
        decoded: dict[int, bytes] = {}
        totalBytes = 0
        totalPixels = 0
        for chart in charts:
            imageBytes = self._decodeChartImage(chart.image_png)
            totalBytes += len(imageBytes)
            # 逐张累加即判：等全部解完再量体，等于让一个请求先把内存占满
            if totalBytes > _MAX_EXPORT_TOTAL_IMAGE_BYTES:
                raise ValidationError(MSG_EXPORT_CHART_TOTAL_TOO_LARGE)
            # 像素同样逐张累加即判，理由同上 —— 只是这条闸拦的是解压炸弹
            pixelCount = self._pngPixelCount(imageBytes)
            if pixelCount is None:
                # 魔数对但读不出 IHDR（截断/畸形）：与其交给 Pillow 赌它报不报错，
                # 不如在边界上按「不是合法 PNG」拒掉。
                raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID)
            if pixelCount > _MAX_EXPORT_IMAGE_PIXELS:
                raise ValidationError(MSG_EXPORT_CHART_IMAGE_TOO_MANY_PIXELS)
            totalPixels += pixelCount
            if totalPixels > _MAX_EXPORT_TOTAL_IMAGE_PIXELS:
                raise ValidationError(MSG_EXPORT_CHART_TOTAL_TOO_MANY_PIXELS)
            decoded[chart.message_id] = imageBytes
        if decoded:
            await self._assertMessagesBelongToSession(
                session, sessionId, list(decoded)
            )
        return decoded

    @staticmethod
    def _decodeChartImage(dataUrl: str) -> bytes:
        """单张 data URL → PNG 字节；前缀/大小/魔数任一不合法抛 ``ValidationError``。"""
        if not dataUrl.startswith(_CHART_IMAGE_DATA_URL_PREFIX):
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID)
        payload = dataUrl[len(_CHART_IMAGE_DATA_URL_PREFIX):]
        # 先用 base64 的长度反推解码后大小（4 字符 → 3 字节）再解码：反过来等于
        # 让一个超长字符串先把内存占住，上限就成了摆设。
        if len(payload) * 3 // 4 > _MAX_EXPORT_IMAGE_BYTES:
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_TOO_LARGE)
        try:
            imageBytes = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID) from None
        if len(imageBytes) > _MAX_EXPORT_IMAGE_BYTES:
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_TOO_LARGE)
        if not imageBytes.startswith(_PNG_MAGIC):
            raise ValidationError(MSG_EXPORT_CHART_IMAGE_INVALID)
        return imageBytes

    @staticmethod
    def _pngPixelCount(imageBytes: bytes) -> int | None:
        """PNG 的像素数（宽 × 高），从 IHDR 头直接读；头不完整/不合法返回 ``None``。

        **为什么手读头而不是用 Pillow 量尺寸**：像素数必须在这一层卡住，而
        ``PIL.Image.open`` 一旦打开就已经按解出的尺寸分配过内存了 —— 让它先解码
        再回头问「你多大」，炸弹早响完了。PNG 的尺寸恒在固定偏移（8 字节签名 +
        4 字节块长 + 4 字节块类型 ⇒ 偏移 16 起 8 个字节），读 8 字节就够，
        也不需要为此把 Pillow 变成这个服务的依赖。
        """
        if len(imageBytes) < 24 or imageBytes[12:16] != b"IHDR":
            return None
        width = int.from_bytes(imageBytes[16:20], "big")
        height = int.from_bytes(imageBytes[20:24], "big")
        # 0 是畸形的（PNG 规范要求 ≥1），照 None 处理，别让 0×N 混过像素闸
        if width <= 0 or height <= 0:
            return None
        return width * height

    @staticmethod
    async def _assertMessagesBelongToSession(
        session: AsyncSession,
        sessionId: str,
        messageIds: list[int],
    ) -> None:
        """这些 messageId 必须真的属于该 session；否则 422（不回显 id）。"""
        stmt = select(SessionMessage.id).where(
            and_(
                SessionMessage.session_id == sessionId,
                SessionMessage.id.in_(messageIds),
            )
        )
        found = (await session.execute(stmt)).scalars().all()
        if len(set(found)) != len(set(messageIds)):
            raise ValidationError(MSG_EXPORT_CHART_MESSAGE_NOT_FOUND)

    @staticmethod
    def attachChartImages(
        payload: ChatExportPayload,
        imagesByMessageId: dict[int, bytes],
    ) -> ChatExportPayload:
        """把位图按 messageId 挂到对应轮次上（返回新 payload，不改入参）。

        没挂上的位图（该轮被 ``_MAX_TURNS_PER_EXPORT`` 截掉、或前端算错了 id）
        **静默忽略**：导出是主功能，多出来一张没人要的图不该让它失败。
        """
        if not imagesByMessageId:
            return payload
        turns = [
            replace(turn, chart_image=imagesByMessageId[turn.message_id])
            if turn.message_id in imagesByMessageId
            else turn
            for turn in payload.turns
        ]
        return replace(payload, turns=turns)

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
                SessionMessage.chart_type,      # 0105：图进导出
                SessionMessage.chart_option,
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
                message_id=asst.id,          # 0105：位图按 id 对轮次
                sql=asst.sql_generated,
                model_name=usage_map.get(asst.id, {}).get("model_name"),
                tokens_used=usage_map.get(asst.id, {}).get("tokens_used"),
                cost=usage_map.get(asst.id, {}).get("cost"),
                # 0105：该轮回答的图表负载（无图为 None）。PDF 里 table/kpi 由后端
                # 原生画，其余 kind 需要前端回传的 PNG（见 pdf_export_service）。
                chart_type=asst.chart_type,
                chart_option=asst.chart_option,
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
            SessionMessage.chart_type,      # 0105：图进导出
            SessionMessage.chart_option,
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
                    message_id=asst_row.id,      # 0105：位图按 id 对轮次
                    sql=asst_row.sql_generated,
                    model_name=meta.get("model_name"),
                    tokens_used=meta.get("tokens_used"),
                    cost=meta.get("cost"),
                    chart_type=asst_row.chart_type,      # 0105
                    chart_option=asst_row.chart_option,
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