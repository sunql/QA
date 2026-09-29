"""评估报告定时调度 worker（feat-dq-evaluation-report，Phase 7b）。

参考 ``agent_scheduler_worker.py`` 的 asyncio + SIGTERM 模式：60s 轮询 PG
``evaluation_report_schedule``，对到期行调
``EvaluationReportSchedulerService.run_one``。

启动：``uv run python -m app.workers.evaluation_report_scheduler_worker``
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Any

from app.services.evaluation_report_scheduler_service import (
    EvaluationReportSchedulerService,
)

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 60.0
_DEFAULT_BATCH_SIZE = 50


class EvaluationReportSchedulerWorker:
    """轮询 evaluation_report_schedule 并执行到期 schedule 的 worker。"""

    def __init__(
        self,
        pollInterval: float = _DEFAULT_POLL_INTERVAL,
        batchSize: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._pollInterval = pollInterval
        self._batchSize = batchSize
        self._stopEvent = asyncio.Event()
        self._svc = EvaluationReportSchedulerService()

    def requestStop(self) -> None:
        self._stopEvent.set()

    async def run(self) -> None:
        from app.infrastructure.database import disposeEngine, getSessionFactory

        factory = getSessionFactory()
        registerSignalHandlers(self.requestStop)
        try:
            logger.info(
                "eval report scheduler 启动（poll=%.0fs batch=%d）",
                self._pollInterval, self._batchSize,
            )
            while not self._stopEvent.is_set():
                try:
                    async with factory() as session:
                        now = datetime.now(timezone.utc)
                        due = await self._svc.due_schedules(session, now)
                        processed = 0
                        for s in due[: self._batchSize]:
                            ok = await self._runOne(session, s.id, now)
                            if ok:
                                processed += 1
                        if processed:
                            logger.info(
                                "批次处理完成：%d 条 schedule 执行", processed,
                            )
                except Exception:
                    logger.exception("eval report scheduler 轮次失败，下轮重试")
                try:
                    await asyncio.wait_for(
                        self._stopEvent.wait(), timeout=self._pollInterval
                    )
                except TimeoutError:
                    pass
        finally:
            await disposeEngine()
            logger.info("eval report scheduler worker 已停止")

    async def _runOne(
        self, session: Any, schedule_id: int, now: datetime,
    ) -> bool:
        try:
            report = await self._svc.run_one(
                session, schedule_id, now,
            )
            if report is None:
                return False
            logger.info(
                "schedule %s 执行成功 → report_id=%s", schedule_id, report.id,
            )
            return True
        except Exception:
            logger.exception("schedule %s 执行异常", schedule_id)
            return False


def registerSignalHandlers(requestStop: Any) -> None:
    def _handler(signum: int, frame: Any) -> None:
        logging.getLogger(__name__).info("收到信号 %s，准备停机", signum)
        requestStop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = EvaluationReportSchedulerWorker()
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())