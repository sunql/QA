"""Agent 定时调度 worker（Phase 7 G5 feat-agent-scheduler）。

独立进程轮询 PG ``agent_schedule``：is_active AND next_run_at <= now 的到期行
→ 条件 UPDATE claim → 调用 AgentRuntimeService.run → 写 agent_run_log。
claim 通过条件 UPDATE（WHERE next_run_at=old）防并发双跑。

启动：uv run python -m app.workers.agent_scheduler_worker
优雅停机：SIGTERM / SIGINT -> 处理完当前批次退出。

每条调度独立事务（per-message commit）：claim 成功则 commit，
失败则 rollback + 跳到下一行。与 audit_worker 的 outbox 消费模式一致。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from app.services.agent_scheduler_service import AgentSchedulerService

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 60.0  # 每分钟轮询一次（cron 精度到分钟）
_DEFAULT_BATCH_SIZE = 50       # 单轮最多处理条数（避免单次长事务锁太久）


class AgentSchedulerWorker:
    """轮询 agent_schedule 并执行到期调度的 worker。"""

    def __init__(
        self,
        pollInterval: float = _DEFAULT_POLL_INTERVAL,
        batchSize: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._pollInterval = pollInterval
        self._batchSize = batchSize
        self._stopEvent = asyncio.Event()
        self._svc = AgentSchedulerService()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def requestStop(self) -> None:
        """请求优雅停机（处理完当前批次后退出 run 循环）。"""
        self._stopEvent.set()

    async def run(self) -> None:
        """主循环：轮询 drain，直到收到停止信号。"""
        from app.infrastructure.database import disposeEngine, getSessionFactory

        factory = getSessionFactory()
        registerSignalHandlers(self.requestStop)
        try:
            logger.info(
                "agent scheduler worker 启动（poll=%.0fs batch=%d）",
                self._pollInterval, self._batchSize,
            )
            while not self._stopEvent.is_set():
                try:
                    async with factory() as session:
                        now = datetime.now(timezone.utc)
                        due = await self._svc.dueSchedules(session, now)
                        processed = 0
                        for schedule in due[: self._batchSize]:
                            ok = await self._runOne(session, schedule, now)
                            if ok:
                                processed += 1
                        if processed:
                            logger.info("批次处理完成：%d 条调度执行", processed)
                except Exception:
                    # DB 不可达等基础设施故障：记日志继续轮询（容器重启策略兜底）
                    logger.exception("agent scheduler 轮次失败，下轮重试")
                try:
                    await asyncio.wait_for(
                        self._stopEvent.wait(), timeout=self._pollInterval
                    )
                except TimeoutError:
                    pass
        finally:
            await disposeEngine()
            logger.info("agent scheduler worker 已停止")

    async def _runOne(
        self, session: Any, schedule: Any, now: datetime
    ) -> bool:
        """执行单条到期调度。成功/失败均独立事务；claim 竞争成功才执行。

        Returns True if the schedule was executed (claimed), False if skipped/not due.
        """
        try:
            log = await self._svc.runSchedule(session, schedule, now)
            if log is None:
                # 未到期或被并发抢占（next_run_at 已前移）
                return False
            if log.status == "success":
                logger.info(
                    "调度 %s 执行成功（agent=%s tokens=%d cost=%.4f actor=%s）",
                    schedule.id, log.agent_code, log.tokens_used, log.cost, log.actor,
                )
            else:
                logger.warning(
                    "调度 %s 执行失败：%s", schedule.id, log.error,
                )
            return True
        except Exception:
            logger.exception("调度 %s 执行异常", schedule.id)
            return False


def registerSignalHandlers(requestStop: Any) -> None:
    """注册 SIGTERM/SIGINT -> 优雅停机（仅主线程可用；测试里不调用）。"""

    def _handler(signum: int, frame: Any) -> None:
        logging.getLogger(__name__).info("收到信号 %s，准备停机", signum)
        requestStop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程（测试并发场景）：跳过注册
            pass


async def main() -> None:
    """进程入口：uv run python -m app.workers.agent_scheduler_worker"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = AgentSchedulerWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
