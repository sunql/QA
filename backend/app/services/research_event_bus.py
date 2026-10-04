"""研究进度事件总线（Task 8）：进程内 per-session 广播（设计 §4.5）。

职责边界：只做「状态机事件 → 订阅者队列」的投递，**不认识 SSE 帧格式**（帧渲染与
心跳在 `api/v1/research.py`）。与 chat 的 SSE 实现完全独立（设计 §4.5 要求独立端点），
不共享任何 chat 域状态。

背压策略**显式化**：每个订阅者一个队列，上限 `QUEUE_MAX`，满则丢**最旧**并 warning ——
慢客户端不得拖慢状态机，状态机也不得因推流失败而中断（`emitEvent` 已做静默兜底）。

进程内单例 `bus`：多进程部署下每进程各自广播（当前后端为单进程；跨进程广播不在
本任务范围，见 task-8-report open concerns）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

QUEUE_MAX = 200
"""每订阅者队列上限（事件条数）；满则丢最旧（背压策略，brief 契约）。"""

EVENT_CONNECTED = "research.connected"
"""流建立后的首事件（Task 8 新增，设计 §4.5 之外）：客户端据此确认通道已就绪。"""

Event = tuple[str, dict[str, Any]]


class ResearchEventBus:
    """进程内 per-session 广播：一个会话可有多个订阅者（多标签页 / 重连）。"""

    def __init__(self, *, maxQueue: int = QUEUE_MAX) -> None:
        self._maxQueue = maxQueue
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = {}

    def subscribe(self, sessionId: str) -> asyncio.Queue[Event]:
        """订阅某会话；返回专属队列（调用方负责 `unsubscribe`，流端在 finally 退订）。"""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxQueue)
        self._subscribers.setdefault(str(sessionId), set()).add(queue)
        return queue

    def unsubscribe(self, sessionId: str, queue: asyncio.Queue[Event]) -> None:
        """退订（幂等）：最后一个订阅者离开时清掉该会话键，字典不无界增长。"""
        key = str(sessionId)
        subscribers = self._subscribers.get(key)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(key, None)

    def subscriberCount(self, sessionId: str) -> int:
        """当前订阅者数（测试与诊断用）。"""
        return len(self._subscribers.get(str(sessionId), ()))

    async def publish(self, sessionId: str, event: str, payload: dict[str, Any]) -> None:
        """广播一帧；队列满的订阅者丢最旧（不阻塞、不抛）。可作 `emit` 的 partial 绑定面。"""
        item: Event = (event, payload)
        for queue in list(self._subscribers.get(str(sessionId), ())):
            self._offer(queue, item, str(sessionId))

    def _offer(self, queue: asyncio.Queue[Event], item: Event, sessionId: str) -> None:
        """入队；满则先丢最旧腾位（背压：新事件优先，丢的是**尚未被消费的旧事件**）。"""
        while True:
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                queue.get_nowait()
                logger.warning(
                    "研究事件队列已满，丢弃最旧事件: session=%s event=%s", sessionId, item[0]
                )


bus = ResearchEventBus()
"""进程内单例：API 层与后台 turn 共用（emit 接线见 `api/v1/research.py`）。"""
