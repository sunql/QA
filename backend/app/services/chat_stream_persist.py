"""流式断连兜底（ChatService 的 StreamPersistMixin）。

`persistInterruptedStream` 是 H4 兜底：把已下发给客户端的部分产出写进历史并
标记中断。由 API 层在 SSE 响应收尾时经 `StreamingResponse(background=...)`
调用——该钩子在 Starlette 的收敛任务组**之外** await（断连时确定会跑到）。
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.schemas import ChatRequest
from app.services.chat_constants import ROUTING_LAYER_L2
from app.services.chat_helpers import streamPersistStateOf
from app.services.messages_zh import MSG_STREAM_INTERRUPTED_EMPTY

logger = logging.getLogger(__name__)


class StreamPersistMixin:
    """流式断连兜底（由 ChatService 组合）。"""

    async def persistInterruptedStream(
        self, dto: ChatRequest, session: AsyncSession,
        *, user: CurrentUser | None = None,  # P0-3 / F1
    ) -> None:
        """断连兜底落库（H4）：把已下发给客户端的部分产出写进历史并标记中断。

        由 API 层在 SSE 响应收尾时经 `StreamingResponse(background=...)` 调用：该钩子
        在 Starlette 的收敛任务组**之外** await（断连时确定会跑到），因此不需要分离任务。

        `session`（请求作用域）**只用来读本轮快照**，写入走**独立会话** —— 实测结论，
        不是防御性写法：取消是在最近一个 await 上打进来的（实测落在本轮 `_recordUsage`
        的 `commit() → flush()` 中途），请求会话随即被标成 needs-rollback（直接写抛
        `PendingRollbackError`）；即便先 `rollback()` 复原，底层 asyncpg 连接也已被关掉
        而 SQLAlchemy 并未察觉（探针实测 `pg_closed=True` 同时 `invalidated=False`），
        下一条语句即整条失败于 `InterfaceError: connection is closed`。注意这不是
        `pool_pre_ping` 能兜住的场景：连接是在**被持有期间**死掉的，pre_ping 只在签出时
        检查。请求会话在取消之后**不是可靠的写入通道**，故不复用：多一条连接，换兜底必达。

        `user`（P0-3 / F1）：调用者身份，用于给兜底落库的 `SessionQueryState` 打
        (user, datasource) scope。默认 None 保持既有调用点兼容（写 NULL scope 行）。

        首行短路：本轮已落库（`pending=False`）⇒ 什么都不写（单发标志去重）。正常跑完
        的请求、以及「先落库后 yield」的各条路径（闲聊/澄清/领域命令/卡片/多步）都在
        落库时已解除标记，故本方法对它们是空操作。

        计量的诚实性：答复 token 只随 `isDone` 终块到达（非终块发 0/0）⇒ 断连时刻部分
        答案的 token 数**根本不存在**，不做回填、不写假账。`token_cost_usd` 只含已测得
        的部分（计划/SQL/图表/已完成的调用），是真实成本的下界；断连那一轮的答复用量行
        随被取消的 flush 一起没了（它本就未提交），SSOT 已注明这是下界而非测得值。
        """
        state = streamPersistStateOf(session)
        if state is None or not state.pending:
            return
        state.pending = False  # 兜底自身也只写一次（background 重复调用/并发都安全）
        answer = "".join(state.answerPieces) or MSG_STREAM_INTERRUPTED_EMPTY

        from app.infrastructure.database import getSessionFactory

        async with getSessionFactory()() as fallbackSession:
            await self._storeSessionMessages(
                fallbackSession, dto.sessionId, dto.question, answer, state.sql,
                routing_layer=ROUTING_LAYER_L2,
                latency_ms=int((time.monotonic() - state.startedAt) * 1000),
                token_cost_usd=state.totalCostUsd,
                interrupted=True,
            )
            # 查询状态照常保存：否则下一轮的追问（REFINE/FOLLOW_UP）失去锚点
            await self._saveQueryState(
                fallbackSession, dto.sessionId,
                question=dto.question, plan=state.plan, sql=state.sql,
                resultColumns=state.resultColumns,
                # F1：断连兜底同样写查询状态，不带 scope 的话下一轮追问丢锚点。
                # `user` 不能像 `_streamQuery` 那样靠形参自动拿到 —— 本方法由 API 层
                # 的 `BackgroundTask` 调用，调用链上只带得到 dto/session，故显式加
                # keyword-only 形参，由 `chat.py` 传 `Depends(getCurrentUser)` 的结果
                # （与同层 `assertSessionOwnership` 用的是同一个对象）。
                userId=user.dbUserId if user is not None else None,
                datasourceId=dto.datasourceId,
            )
        logger.info(
            "流式断连兜底落库: session=%s 已下发片段=%d 内容长度=%d",
            dto.sessionId, len(state.answerPieces), len(answer),
        )


__all__ = ["StreamPersistMixin"]
