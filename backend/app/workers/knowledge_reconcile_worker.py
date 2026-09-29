"""知识三库对账每日 worker（v3.1 任务 A5，模式参照 agent_scheduler_worker）。

独立进程轮询到期（cron ``0 3 * * *``，借 agent_scheduler_service 的 croniter
模式）→ 调用 ``KnowledgeCompilerService.reconcile``（只读巡检，结果落
audit_history）。

启动：uv run python -m app.workers.knowledge_reconcile_worker
优雅停机：SIGTERM / SIGINT → 处理完当前轮次退出。

测试环境不真挂 cron：调度注册（启动本 worker 进程）是部署层动作，
worker 可注入 service 替身（svc 参数），单测只覆盖纯函数（isReconcileDue）。
worker 重启后 _lastRunAt 归零 → 重启后首个轮次若已过当日触发点会补跑一次
对账；reconcile 只读 + audit 追加，补跑无害。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from app.services.knowledge_compiler_service import (
    _DAILY_RECONCILE_CRON,
    KnowledgeCompilerService,
)

logger = logging.getLogger(__name__)

# 到期检查轮询间隔（cron 精度到分钟，5 分钟余量充足）
_DEFAULT_POLL_INTERVAL = 300.0


def _registerSignalHandlers(requestStop: Any) -> None:
    """注册 SIGTERM/SIGINT -> 优雅停机（仅主线程可用；测试里不调用）。"""

    def _handler(signum: int, frame: Any) -> None:
        logger.info("收到信号 %s，准备停机", signum)
        requestStop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程（测试并发场景）：跳过注册
            pass


class KnowledgeReconcileWorker:
    """轮询每日对账到期并执行的 worker。"""

    def __init__(
        self,
        *,
        pollInterval: float = _DEFAULT_POLL_INTERVAL,
        svc: KnowledgeCompilerService | None = None,
    ) -> None:
        self._pollInterval = pollInterval
        self._svc = svc or KnowledgeCompilerService()
        self._stopEvent = asyncio.Event()
        self._lastRunAt: datetime | None = None

    def requestStop(self) -> None:
        """请求优雅停机（处理完当前轮次后退出 run 循环）。"""
        self._stopEvent.set()

    async def run(self) -> None:
        """主循环：到期即对账，直到收到停止信号。"""
        from app.infrastructure.database import getSessionFactory

        factory = getSessionFactory()
        _registerSignalHandlers(self.requestStop)
        logger.info(
            "knowledge reconcile worker 启动（poll=%.0fs cron=%s）",
            self._pollInterval, _DAILY_RECONCILE_CRON,
        )
        while not self._stopEvent.is_set():
            try:
                await self._tick(factory)
            except Exception:
                # 基础设施故障：记日志继续轮询（容器重启策略兜底）
                logger.exception("knowledge reconcile 轮次失败，下轮重试")
            try:
                await asyncio.wait_for(
                    self._stopEvent.wait(), timeout=self._pollInterval
                )
            except asyncio.TimeoutError:
                pass  # 正常轮询节奏

    async def _tick(self, factory) -> None:
        """单轮：到期检查 → reconcile → 记录本轮时间。"""
        now = datetime.now(timezone.utc)
        if not self._svc.isReconcileDue(self._lastRunAt, now):
            return
        async with factory() as session:
            summary = await self._svc.reconcile(session)
        self._lastRunAt = now
        logger.info("每日知识对账完成: %s", summary.toDict())


async def _main() -> None:
    worker = KnowledgeReconcileWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
